"""Fleet server: MQTT ingest -> live fleet state -> WebSocket dashboard.

Run:  python -m server.main          (from the fleet-server/ directory)
Then: tailscale funnel 8080          (to publish it on the internet)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, quote

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

from .alerts import AlertEngine, send_to
from .broker import MqttBroker
from .db import Database
from .history import TelemetryWriter, query_events, query_telemetry
from .ingest import MqttIngest, TelemetryRouter
from .metrics import EgressMeter
from .registry import Registry
from .state import FleetState

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fleet")


# --------------------------------------------------------------------- config

DEFAULTS: Dict[str, Any] = {
    "http": {"host": "0.0.0.0", "port": 8080, "password": None,
             "session_days": 30, "max_attempts": 10, "attempt_window_min": 5},
    "mqtt": {
        "embedded": True,
        "host": "127.0.0.1",
        "port": 1883,
        "bind": "0.0.0.0",
        "public_host": None,
        "require_auth": True,
        "username": None,
        "password": None,
        "topic": "fleet/+/status",
        "lwt_topic": "fleet/+/lwt",
        "raw_topic": "fleet/+/raw",
        "command_topic": "fleet/{id}/cmd",
    },
    "dashboard": {
        "push_hz": 4.0,
        "offline_after": 6.0,
        "history_points": 240,      # per-robot trail kept in memory
        "ws_ping_s": 20,            # WebSocket keepalive
    },
    "registry": {
        "token_ttl_min": 30,        # enrollment token lifetime
        "credential_cache_s": 30,   # how long the broker trusts its cached creds
    },
    "alerts": {"enabled": True, "channels": {}},
    "bwtest": {"enabled": True, "max_mb": 64},
    "database": {
        "enabled": True,
        "dsn": "postgresql://itri:CHANGE_ME@127.0.0.1:5432/itri_fleet",
        "retention_days": 31,
        "flush_interval_s": 1.0,
        "buffer_max": 200000,           # telemetry rows held if the DB is down
        "topic_buffer_max": 400000,     # relayed topic rows held if the DB is down
        "event_buffer_max": 20000,
        "partition_days_ahead": 2,      # create partitions this far in advance
    },
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def warn_if_port_taken(port: int) -> None:
    """Another broker on 127.0.0.1:<port> steals every loopback client.

    We bind 0.0.0.0 so the listen still succeeds, but tools connecting to
    localhost (the simulator, mosquitto_pub, anything on this machine) land on
    the other broker instead of ours -- while robots on the LAN/tailnet land on
    ours.  Say so loudly rather than leaving a half-working split.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        if probe.connect_ex(("127.0.0.1", int(port))) != 0:
            return

    log.warning("=" * 68)
    log.warning("another MQTT broker is already listening on 127.0.0.1:%d", port)
    log.warning("this server ingests in-process, so the dashboard is unaffected,")
    log.warning("but localhost publishers will reach THAT broker, not this one.")
    log.warning("point local tools at the LAN/tailnet IP, stop the other broker,")
    log.warning("or set mqtt.embedded=false in config.yaml to use it instead.")
    log.warning("=" * 68)


def tailscale_ip() -> Optional[str]:
    """This machine's Tailscale address, discovered at runtime.

    Hard-coding it in config.yaml makes the file machine-specific, which is
    exactly what bites you when the server moves to different hardware.
    """
    import socket

    for name in socket.gethostbyname_ex(socket.gethostname())[2]:
        if name.startswith("100."):
            return name
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=8).stdout.strip().splitlines()
        return out[0].strip() if out else None
    except Exception:
        return None


def resolve_placeholders(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """`tailscale` in an address field means "whatever this host's tailnet IP is"."""
    mq = cfg["mqtt"]
    for key in ("bind", "public_host"):
        if str(mq.get(key)).lower() == "tailscale":
            ip = tailscale_ip()
            if ip:
                mq[key] = ip
                log.info("mqtt.%s = %s (detected Tailscale address)", key, ip)
            else:
                log.error("mqtt.%s: 'tailscale' requested but no 100.x address found "
                          "-- is Tailscale connected?", key)
                mq[key] = "0.0.0.0" if key == "bind" else None
    return cfg


def load_config() -> Dict[str, Any]:
    path = Path(os.environ.get("FLEET_CONFIG", ROOT / "config.yaml"))
    cfg = DEFAULTS
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            cfg = deep_merge(DEFAULTS, yaml.safe_load(fh) or {})
    # env wins, so you can override without editing the file
    if os.environ.get("FLEET_PORT"):
        cfg["http"]["port"] = int(os.environ["FLEET_PORT"])
    if os.environ.get("FLEET_PASSWORD"):
        cfg["http"]["password"] = os.environ["FLEET_PASSWORD"]
    if os.environ.get("FLEET_MQTT_HOST"):
        cfg["mqtt"]["host"] = os.environ["FLEET_MQTT_HOST"]
        cfg["mqtt"]["embedded"] = False
    # keep the DB password out of the config file when deploying for real
    if os.environ.get("FLEET_DB_DSN"):
        cfg["database"]["dsn"] = os.environ["FLEET_DB_DSN"]
    return resolve_placeholders(cfg)


CFG = load_config()
PASSWORD: Optional[str] = CFG["http"]["password"]
SESSION_MAX_AGE = int(float(CFG["http"]["session_days"]) * 86400)
COOKIE = "itri_session"

# sid -> expiry (unix seconds). In-memory: a server restart logs everyone out,
# which is the right trade for never persisting a credential to disk.
SESSIONS: Dict[str, float] = {}
# client -> timestamps of recent failed logins
ATTEMPTS: Dict[str, List[float]] = {}

state = FleetState(offline_after=float(CFG["dashboard"]["offline_after"]),
                   history_len=int(CFG["dashboard"]["history_points"]))
meter = EgressMeter()
broker: Optional[MqttBroker] = None
ingest: Optional[MqttIngest] = None
db: Optional[Database] = None
writer: Optional[TelemetryWriter] = None
registry: Optional[Registry] = None
alerts: Optional[AlertEngine] = None

# Archival hooks are wired only when the database is enabled; without them the
# router just updates live state, exactly as before.
router = TelemetryRouter(
    state,
    on_sample=lambda robot: writer.record(robot) if writer else None,
    on_presence=lambda rid, up: writer.note_presence(rid, up) if writer else None,
    on_raw=lambda rid, batch: _on_raw_batch(rid, batch),
)


def _on_raw_batch(robot_id: str, batch: list) -> None:
    """Relayed topics feed both the archive and the alert engine."""
    if writer:
        writer.record_topics(robot_id, batch)
    if alerts:
        alerts.observe_topics(robot_id, batch)


# ------------------------------------------------------------ websocket hub

class Hub:
    """Tracks dashboard sockets and pushes only what changed since last frame.

    Sending deltas instead of full snapshots is what keeps Funnel egress flat:
    an idle fleet costs a couple of hundred bytes a second, not tens of KB.
    """

    def __init__(self) -> None:
        self.clients: Dict[WebSocket, Dict[str, int]] = {}

    async def join(self, ws: WebSocket) -> None:
        self.clients[ws] = {}
        await self.send(ws, {
            "t": "snapshot",
            "ts": time.time(),
            "robots": state.snapshot(),
            "summary": state.summary(),
        })
        self.clients[ws] = {r.id: r.rev for r in state.robots.values()}

    def leave(self, ws: WebSocket) -> None:
        self.clients.pop(ws, None)

    async def send(self, ws: WebSocket, msg: Dict[str, Any]) -> bool:
        data = json.dumps(msg, separators=(",", ":"), default=str)
        try:
            await ws.send_text(data)
        except Exception:
            return False
        meter.add_ws(len(data))
        return True

    async def push(self) -> None:
        if not self.clients:
            return
        summary = state.summary()
        now = time.time()
        for ws, seen in list(self.clients.items()):
            changed = [r.to_dict() for r in state.robots.values() if seen.get(r.id) != r.rev]
            gone = [rid for rid in seen if rid not in state.robots]
            if not changed and not gone:
                continue
            ok = await self.send(ws, {"t": "update", "ts": now, "robots": changed,
                                      "removed": gone, "summary": summary})
            if not ok:
                self.leave(ws)
                continue
            for r in changed:
                seen[r["id"]] = r["rev"]
            for rid in gone:
                seen.pop(rid, None)

    async def broadcast_metrics(self) -> None:
        if not self.clients:
            return
        snap = meter.snapshot(ws_clients=len(self.clients))
        snap["mqtt_connected"] = mqtt_healthy()
        for ws in list(self.clients):
            if not await self.send(ws, {"t": "metrics", "metrics": snap}):
                self.leave(ws)


hub = Hub()


def mqtt_healthy() -> bool:
    """Embedded ingest cannot disconnect from itself; external can."""
    if broker is not None:
        return True
    return bool(ingest and ingest.connected)


# ----------------------------------------------------------- background tasks

async def pusher_task() -> None:
    """Single ticker: expire stale robots, push deltas, publish egress metrics.

    There is no periodic full resync on purpose -- a client's rev map can only
    drift if a send fails, and a failed send drops the client, which makes the
    browser reconnect and take a fresh snapshot.
    """
    period = 1.0 / max(float(CFG["dashboard"]["push_hz"]), 0.2)
    last_metrics = 0.0
    while True:
        try:
            await asyncio.sleep(period)
            for rid in state.reap():        # telemetry went stale -> offline
                if writer:
                    writer.note_presence(rid, False)
            if alerts:
                await alerts.tick()
            await hub.push()
            if time.time() - last_metrics >= 2.0:
                last_metrics = time.time()
                await hub.broadcast_metrics()
                # picks up newly enrolled robots without a restart
                if registry is not None:
                    await registry.ensure_cache()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pusher loop error")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global broker, ingest, db, writer, registry, alerts
    loop = asyncio.get_running_loop()
    mq = CFG["mqtt"]
    dbc = CFG["database"]

    if dbc["enabled"]:
        db = Database(dbc["dsn"], retention_days=int(dbc["retention_days"]),
                      partition_days_ahead=int(dbc["partition_days_ahead"]))
        await db.start()          # never raises; a dead DB is a degraded mode
        writer = TelemetryWriter(db, flush_interval=float(dbc["flush_interval_s"]),
                                 buffer_max=int(dbc["buffer_max"]),
                                 event_buffer_max=int(dbc["event_buffer_max"]),
                                 topic_buffer_max=int(dbc["topic_buffer_max"]))
        writer.start()
        writer.note_event(None, "system", {"msg": "server started"}, "info")
        registry = Registry(db, cache_ttl=float(CFG["registry"]["credential_cache_s"]))
        if db.ready:
            await registry.refresh_cache()
        if CFG["alerts"]["enabled"]:
            alerts = AlertEngine(state, db, writer, CFG["alerts"]["channels"])
            await alerts.refresh_rules(force=True)
            live = [n for n, c in (CFG["alerts"]["channels"] or {}).items()
                    if c and c.get("enabled")]
            log.info("alerts on: %d rules, channels=%s",
                     len(alerts.rules), ", ".join(live) or "none enabled")

    if mq["embedded"]:
        warn_if_port_taken(mq["port"])
        auth_fn = acl_fn = None
        if mq["require_auth"]:
            if registry is None:
                log.error("mqtt.require_auth needs the database; refusing to start "
                          "the broker wide open -- fix the DB or set require_auth: false")
                raise RuntimeError("require_auth enabled but registry unavailable")
            auth_fn = registry.verify_cached
            acl_fn = Registry.topic_allowed
            log.info("MQTT auth: per-robot credentials + topic ACL (%d enrolled)",
                     len(registry.known_ids()))
        else:
            log.warning("MQTT auth DISABLED -- anyone who can reach port %d can "
                        "forge telemetry for any robot", mq["port"])
        broker = MqttBroker(host=mq["bind"], port=int(mq["port"]),
                            username=mq["username"], password=mq["password"],
                            authenticate=auth_fn, authorize=acl_fn)
        await broker.start()
        # in-process delivery -- see server/ingest.py for why this is not a
        # loopback paho client
        broker.subscribe_inproc(mq["topic"], router.handle)
        broker.subscribe_inproc(mq["lwt_topic"], router.handle)
        broker.subscribe_inproc(mq["raw_topic"], router.handle)
        log.info("ingest: in-process from embedded broker (%s, %s)",
                 mq["topic"], mq["lwt_topic"])
    else:
        ingest = MqttIngest(router, loop, host=mq["host"], port=int(mq["port"]),
                            topic=mq["topic"], lwt_topic=mq["lwt_topic"],
                            username=mq["username"], password=mq["password"])
        ingest.start()

    task = asyncio.create_task(pusher_task())
    log.info("dashboard on http://localhost:%d  (auth: %s)", CFG["http"]["port"],
             "password required" if PASSWORD else "OPEN -- no password set")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if ingest:
            ingest.stop()
        if broker:
            await broker.stop()
        if writer:
            writer.note_event(None, "system", {"msg": "server stopping"}, "info")
            await writer.stop()
        if db:
            await db.stop()


app = FastAPI(title="Fleet Server", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ auth/meter

# /api/enroll is intentionally open: the one-time token IS the credential, and
# the robot redeeming it has no dashboard password. It is rate limited on the
# same per-IP counter as failed logins.
OPEN_PATHS = {"/healthz", "/login", "/logout", "/favicon.ico", "/api/enroll"}
OPEN_PREFIXES = ("/agent/",)     # wheel download; contains no secrets


def new_session() -> str:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    SESSIONS[sid] = now + SESSION_MAX_AGE
    for old, exp in list(SESSIONS.items()):  # cheap sweep, keeps the dict bounded
        if exp < now:
            del SESSIONS[old]
    return sid


def session_ok(sid: Optional[str]) -> bool:
    if not sid:
        return False
    exp = SESSIONS.get(sid)
    if exp is None:
        return False
    if exp < time.time():
        SESSIONS.pop(sid, None)
        return False
    return True


def password_ok(supplied: Optional[str]) -> bool:
    return bool(supplied) and bool(PASSWORD) and \
        secrets.compare_digest(str(supplied), str(PASSWORD))


def client_key(request: Request) -> str:
    """Behind Funnel every request looks like 127.0.0.1, so this degrades to a
    global limiter -- which is the safe direction for a brute-force guard."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def rate_limited(key: str) -> bool:
    window = float(CFG["http"]["attempt_window_min"]) * 60
    now = time.time()
    hits = [t for t in ATTEMPTS.get(key, []) if now - t < window]
    ATTEMPTS[key] = hits
    return len(hits) >= int(CFG["http"]["max_attempts"])


def note_failure(key: str) -> None:
    ATTEMPTS.setdefault(key, []).append(time.time())


def authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    if session_ok(request.cookies.get(COOKIE)):
        return True
    # header/query credential for scripts (robots, bw_probe, curl)
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return password_ok(bearer) or password_ok(request.query_params.get("token"))


@app.middleware("http")
async def gate_and_meter(request: Request, call_next):
    path = request.url.path
    is_open = path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)
    if PASSWORD and not is_open and not authed(request):
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            nxt = request.url.path
            if request.url.query:
                nxt += "?" + request.url.query
            response = RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=303)
        else:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
        meter.add_http(200)
        return response

    response = await call_next(request)
    size = int(response.headers.get("content-length") or 0)
    meter.add_http(size + 200)  # +200 rough header overhead, so the meter is honest
    return response


def ws_authed(ws: WebSocket) -> bool:
    if not PASSWORD:
        return True
    if session_ok(ws.cookies.get(COOKIE)):
        return True
    return password_ok(ws.query_params.get("token"))


# ---------------------------------------------------------------------- routes

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(WEB / "index.html")


def render_login(message: str = "", nxt: str = "/", status: int = 200) -> HTMLResponse:
    html = (WEB / "login.html").read_text(encoding="utf-8")
    html = (html.replace("<!--MESSAGE-->", escape(message))
                .replace("<!--MSGCLASS-->", "msg show" if message else "msg")
                .replace("<!--NEXT-->", escape(nxt, quote=True)))
    return HTMLResponse(html, status_code=status)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if not PASSWORD or authed(request):
        return RedirectResponse(next or "/", status_code=303)
    return render_login(nxt=next or "/")


@app.post("/login")
async def login_submit(request: Request):
    nxt = request.query_params.get("next") or "/"
    key = client_key(request)
    if rate_limited(key):
        return render_login("嘗試次數過多,請稍後再試。", nxt, status=429)

    # parsed by hand so the project does not need python-multipart
    body = (await request.body()).decode("utf-8", "replace")
    supplied = (parse_qs(body).get("password") or [""])[0]
    nxt = (parse_qs(body).get("next") or [nxt])[0] or "/"

    if not password_ok(supplied):
        note_failure(key)
        log.warning("failed login from %s", key)
        return render_login("密碼錯誤。", nxt, status=401)

    ATTEMPTS.pop(key, None)
    if not nxt.startswith("/"):
        nxt = "/"  # never bounce a login into an off-site URL
    # Funnel terminates TLS and proxies over plain loopback, so uvicorn always
    # sees http -- trust the forwarded scheme, else a Secure cookie would never
    # be set in production and never work on localhost.
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response = RedirectResponse(nxt, status_code=303)
    response.set_cookie(COOKIE, new_session(), max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="lax", secure=https, path="/")
    log.info("login ok from %s", key)
    return response


@app.get("/logout")
async def logout(request: Request):
    SESSIONS.pop(request.cookies.get(COOKIE) or "", None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/api/fleet")
async def api_fleet():
    return {"ts": time.time(), "summary": state.summary(), "robots": state.snapshot()}


@app.get("/api/robot/{robot_id}")
async def api_robot(robot_id: str):
    robot = state.robots.get(robot_id)
    if robot is None:
        raise HTTPException(404, "unknown robot")
    return robot.to_dict(with_history=True)


@app.get("/api/summary")
async def api_summary():
    return state.summary()


@app.get("/api/metrics")
async def api_metrics():
    snap = meter.snapshot(ws_clients=len(hub.clients))
    snap["fleet"] = state.summary()
    snap["mqtt"] = {
        "connected": mqtt_healthy(),
        "mode": "embedded-inproc" if broker else "external-paho",
        "error": ingest.last_error if ingest else None,
        "accepted": router.accepted,
        "dropped": router.dropped,
        "broker_stats": broker.stats if broker else None,
    }
    snap["history"] = writer.stats() if writer else {"enabled": False}
    return snap


# ------------------------------------------------------------------- history

def _need_db() -> Database:
    if db is None or not db.ready:
        raise HTTPException(503, f"history unavailable: {db.last_error if db else 'disabled'}")
    return db


def _parse_time(value: Optional[str], default: datetime) -> datetime:
    if not value:
        return default
    try:                                   # accept epoch seconds or ISO-8601
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, f"bad timestamp: {value!r}")


@app.get("/api/history")
async def api_history(robot_id: Optional[str] = None,
                      start: Optional[str] = None, end: Optional[str] = None,
                      limit: int = 5000, bucket_s: Optional[float] = None):
    """Archived telemetry. Defaults to the last hour.

    Pass bucket_s to average into time buckets -- required for wide windows,
    otherwise a month-long query would try to return millions of rows.
    """
    database = _need_db()
    now = datetime.now(timezone.utc)
    t1 = _parse_time(end, now)
    t0 = _parse_time(start, t1 - timedelta(hours=1))
    if t0 >= t1:
        raise HTTPException(400, "start must be before end")
    limit = max(1, min(int(limit), 50_000))

    # auto-bucket anything wider than 6h so the response stays sane
    span = (t1 - t0).total_seconds()
    if bucket_s is None and span > 6 * 3600:
        bucket_s = max(span / limit, 1.0)

    rows = await query_telemetry(database, robot_id, t0, t1, limit, bucket_s)
    return {"start": t0, "end": t1, "bucket_s": bucket_s,
            "count": len(rows), "rows": rows}


# -------------------------------------------------------------- PWA / push

@app.get("/manifest.json")
async def manifest():
    return FileResponse(WEB / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # Must be served from the root so its scope covers the whole site, and must
    # not be cached or an old worker sticks around after a deploy.
    return FileResponse(WEB / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})


@app.get("/api/push/key")
async def push_key():
    conf = (CFG["alerts"]["channels"] or {}).get("push") or {}
    if not conf.get("enabled") or not conf.get("public_key"):
        raise HTTPException(404, "web push not configured")
    return {"public_key": conf["public_key"]}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, body: Dict[str, Any]):
    database = _need_db()
    keys = body.get("keys") or {}
    endpoint = body.get("endpoint")
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "malformed subscription")
    await database.execute("""
        INSERT INTO push_subscriptions (endpoint, p256dh, auth, label, user_agent)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (endpoint) DO UPDATE
        SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,
            label = COALESCE(EXCLUDED.label, push_subscriptions.label),
            failures = 0
    """, (endpoint, keys["p256dh"], keys["auth"], body.get("label"),
          request.headers.get("user-agent", "")[:300]))
    n = (await database.fetch("SELECT count(*) AS n FROM push_subscriptions"))[0]["n"]
    return {"ok": True, "devices": n}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: Dict[str, Any]):
    database = _need_db()
    await database.execute("DELETE FROM push_subscriptions WHERE endpoint = %s",
                           (body.get("endpoint"),))
    return {"ok": True}


@app.get("/api/push/devices")
async def push_devices():
    database = _need_db()
    rows = await database.fetch(
        "SELECT endpoint, label, user_agent, created_at, last_ok, failures"
        " FROM push_subscriptions ORDER BY created_at")
    for r in rows:
        r["endpoint"] = r["endpoint"][:60] + "…"
    return {"count": len(rows), "devices": rows}


# ------------------------------------------------------------- alert rules

FIELD_KEYS = ["battery", "state", "v", "w", "temp", "wifi", "odom", "errors", "age"]


def _need_alerts() -> AlertEngine:
    if alerts is None:
        raise HTTPException(503, "alerts disabled")
    return alerts


@app.get("/api/alerts/rules")
async def list_rules():
    database = _need_db()
    rows = await database.fetch("SELECT * FROM alert_rules ORDER BY id")
    return {"count": len(rows), "rules": rows,
            "fields": FIELD_KEYS,
            "channels": [n for n, c in (CFG["alerts"]["channels"] or {}).items()
                         if c and c.get("enabled")]}


@app.post("/api/alerts/rules")
async def create_rule(body: Dict[str, Any]):
    database = _need_db()
    required = ("name", "source", "op")
    if any(not body.get(k) for k in required):
        raise HTTPException(400, f"missing one of {required}")
    rows = await database.fetch("""
        INSERT INTO alert_rules (name, enabled, robot_id, source, key, op, value,
                                 value2, text_value, for_seconds, clear_value,
                                 severity, cooldown_min, channels, message_template)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (body["name"], body.get("enabled", True), body.get("robot_id") or None,
          body["source"], body.get("key") or "", body["op"],
          body.get("value"), body.get("value2"), body.get("text_value"),
          float(body.get("for_seconds", 10)), body.get("clear_value"),
          body.get("severity", "warn"), float(body.get("cooldown_min", 15)),
          body.get("channels"), body.get("message_template") or None))
    await _need_alerts().refresh_rules(force=True)
    return {"ok": True, "id": rows[0]["id"]}


@app.patch("/api/alerts/rules/{rule_id}")
async def update_rule(rule_id: int, body: Dict[str, Any]):
    database = _need_db()
    allowed = {"name", "enabled", "robot_id", "source", "key", "op", "value",
               "value2", "text_value", "for_seconds", "clear_value", "severity",
               "cooldown_min", "channels", "message_template"}
    sets, params = [], []
    for k, v in body.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            params.append(v if v != "" else None)
    if not sets:
        raise HTTPException(400, "nothing to update")
    sets.append("updated_at = now()")
    params.append(rule_id)
    n = await database.execute(
        f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = %s", params)
    if not n:
        raise HTTPException(404, "unknown rule")
    await _need_alerts().refresh_rules(force=True)
    return {"ok": True}


@app.delete("/api/alerts/rules/{rule_id}")
async def delete_rule(rule_id: int):
    database = _need_db()
    await database.execute("DELETE FROM alert_rules WHERE id = %s", (rule_id,))
    await _need_alerts().refresh_rules(force=True)
    return {"ok": True}


@app.get("/api/alerts/active")
async def active_alerts():
    engine = _need_alerts()
    return {"open": engine.open_alerts(),
            "fired": engine.fired, "resolved": engine.resolved,
            "notify_failures": engine.notify_failures,
            "last_error": engine.last_error}


@app.get("/api/alerts/history")
async def alert_history(limit: int = 100, robot_id: Optional[str] = None):
    database = _need_db()
    where, params = ("WHERE robot_id = %s", [robot_id]) if robot_id else ("", [])
    params.append(max(1, min(int(limit), 1000)))
    rows = await database.fetch(f"""
        SELECT id, rule_name, robot_id, severity, message, value,
               started_at, resolved_at, notified
        FROM alerts {where} ORDER BY started_at DESC LIMIT %s
    """, params)
    return {"count": len(rows), "alerts": rows}


@app.post("/api/alerts/test")
async def test_notify(body: Dict[str, Any]):
    """Fire a real notification through one channel, so you find out it is
    misconfigured now rather than during an actual incident."""
    name = body.get("channel")
    conf = (CFG["alerts"]["channels"] or {}).get(name)
    if not conf:
        raise HTTPException(404, f"unknown channel {name!r}")
    if not conf.get("enabled"):
        raise HTTPException(400, f"channel {name!r} is disabled in config.yaml")
    try:
        await send_to(name, conf, "[TEST] ITRI Fleet",
                      body.get("message") or "測試通知 — 收到代表這個管道可用。",
                      "warn", False, db=db)
    except Exception as exc:
        raise HTTPException(502, f"{type(exc).__name__}: {exc}")
    return {"ok": True, "channel": name}


@app.get("/api/topics")
async def api_topics(robot_id: Optional[str] = None):
    """Every topic each robot has relayed, for mapping in the admin UI.

    The agent forwards blindly, so this catalogue is how you find out what a
    new chassis model actually publishes -- without reading its documentation.
    """
    database = _need_db()
    where, params = ("WHERE robot_id = %s", [robot_id]) if robot_id else ("", [])
    rows = await database.fetch(f"""
        SELECT robot_id, topic, samples, last_value, first_seen, last_seen
        FROM topic_catalog {where}
        ORDER BY robot_id, topic
    """, params)
    return {"count": len(rows), "topics": rows}


@app.get("/api/topic_history")
async def api_topic_history(topic: str, robot_id: Optional[str] = None,
                            start: Optional[str] = None, end: Optional[str] = None,
                            limit: int = 5000, bucket_s: Optional[float] = None):
    """Values over time for one relayed topic."""
    database = _need_db()
    now = datetime.now(timezone.utc)
    t1 = _parse_time(end, now)
    t0 = _parse_time(start, t1 - timedelta(hours=1))
    limit = max(1, min(int(limit), 50_000))
    span = (t1 - t0).total_seconds()
    if bucket_s is None and span > 6 * 3600:
        bucket_s = max(span / limit, 1.0)

    where = ["ts >= %s", "ts < %s", "topic = %s"]
    params: List[Any] = [t0, t1, topic]
    if robot_id:
        where.append("robot_id = %s")
        params.append(robot_id)
    clause = " AND ".join(where)
    params.append(limit)

    if bucket_s:
        rows = await database.fetch(f"""
            SELECT robot_id,
                   to_timestamp(floor(extract(epoch FROM ts) / {float(bucket_s)})
                                * {float(bucket_s)}) AS ts,
                   avg(num)::real AS num, min(num)::real AS lo, max(num)::real AS hi,
                   count(*) AS samples
            FROM topic_samples WHERE {clause}
            GROUP BY robot_id, 2 ORDER BY 2 LIMIT %s
        """, params)
    else:
        rows = await database.fetch(f"""
            SELECT robot_id, ts, num, payload
            FROM topic_samples WHERE {clause}
            ORDER BY ts LIMIT %s
        """, params)
    return {"topic": topic, "start": t0, "end": t1, "bucket_s": bucket_s,
            "count": len(rows), "rows": rows}


@app.get("/api/events")
async def api_events(robot_id: Optional[str] = None, kind: Optional[str] = None,
                     severity: Optional[str] = None,
                     start: Optional[str] = None, end: Optional[str] = None,
                     limit: int = 500):
    database = _need_db()
    rows = await query_events(
        database, robot_id, kind, severity,
        _parse_time(start, None) if start else None,
        _parse_time(end, None) if end else None,
        max(1, min(int(limit), 5000)))
    return {"count": len(rows), "events": rows}


# ------------------------------------------------------------------ registry

def _need_registry() -> Registry:
    if registry is None or db is None or not db.ready:
        raise HTTPException(503, "registry unavailable: database is down")
    return registry


AGENT_DIR = ROOT / "agent" / "dist"
# Optional. A Tailscale auth key is a credential, so it is only baked into the
# displayed install command if you deliberately put one in config.yaml;
# otherwise the operator sees a placeholder to fill in.
TAILSCALE_AUTHKEY_HINT = CFG.get("tailscale", {}).get("authkey") or "tskey-auth-XXXXX"


def agent_wheel() -> Optional[Path]:
    """Newest built wheel, or None if the agent has not been packaged yet."""
    wheels = sorted(AGENT_DIR.glob("itri_fleet_agent-*.whl"))
    return wheels[-1] if wheels else None


def install_command(base_url: str, token: str) -> Dict[str, Any]:
    """The exact lines to paste on a new vehicle's onboard computer."""
    wheel = agent_wheel()
    steps = [
        "curl -fsSL https://tailscale.com/install.sh | sh",
        f"sudo tailscale up --authkey={TAILSCALE_AUTHKEY_HINT} --advertise-tags=tag:robot",
    ]
    # Raspberry Pi OS Bookworm and Debian 12 enforce PEP 668: pip refuses to
    # install into the system Python at all. pipx puts the CLI in its own
    # environment and still exposes `itri-agent` on PATH, which is what we want.
    url = f"{base_url}/agent/{wheel.name}" if wheel else "itri-fleet-agent"
    steps.append("sudo apt install -y pipx && pipx ensurepath")
    steps.append(f"pipx install {url}")
    steps.append(f"itri-agent enroll --server {base_url} --token {token}")
    steps.append("itri-agent discover")
    steps.append("itri-agent run")
    return {"steps": steps, "oneliner": steps[-3],
            "note": "Bookworm 之後 pip 不能裝進系統 Python(PEP 668),所以用 pipx"}


@app.get("/api/admin/robots")
async def admin_list_robots():
    reg = _need_registry()
    robots = await reg.list_robots()
    live = {r.id: r for r in state.robots.values()}
    for row in robots:
        r = live.get(row["id"])
        row["live"] = {"online": r.online, "state": r.state, "battery": r.battery} if r else None
    return {"count": len(robots), "robots": robots}


@app.post("/api/admin/robots")
async def admin_create_robot(request: Request, body: Dict[str, Any]):
    reg = _need_registry()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        created = await reg.create_robot(
            name=name,
            robot_id=(body.get("id") or "").strip() or None,
            tags=body.get("tags") or [],
            ttl_minutes=int(body.get("ttl_minutes")
                            or CFG["registry"]["token_ttl_min"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if writer:
        writer.note_event(created["id"], "enroll", {"action": "created", "name": name})
    base = str(request.base_url).rstrip("/")
    created["install"] = install_command(base, created["token"])
    return created


@app.post("/api/admin/robots/{robot_id}/token")
async def admin_reissue_token(robot_id: str, request: Request):
    reg = _need_registry()
    if not await reg.get(robot_id):
        raise HTTPException(404, "unknown robot")
    tok = await reg.issue_token(robot_id)
    if writer:
        writer.note_event(robot_id, "enroll", {"action": "token_reissued"})
    tok["install"] = install_command(str(request.base_url).rstrip("/"), tok["token"])
    return tok


@app.post("/api/admin/robots/{robot_id}/revoke")
async def admin_revoke(robot_id: str):
    reg = _need_registry()
    if not await reg.get(robot_id):
        raise HTTPException(404, "unknown robot")
    await reg.revoke(robot_id)
    # refresh before kicking, so a racing reconnect cannot slip back in on the
    # stale cache entry
    await reg.refresh_cache()
    kicked = broker.kick(robot_id) if broker else 0
    if writer:
        writer.note_event(robot_id, "revoke",
                          {"action": "revoked", "sessions_killed": kicked}, "warn")
    return {"ok": True, "id": robot_id, "sessions_killed": kicked}


@app.patch("/api/admin/robots/{robot_id}")
async def admin_update(robot_id: str, body: Dict[str, Any]):
    reg = _need_registry()
    if not await reg.get(robot_id):
        raise HTTPException(404, "unknown robot")
    await reg.update(robot_id, name=body.get("name"), tags=body.get("tags"),
                     display=body.get("display"), notes=body.get("notes"))
    return {"ok": True, "id": robot_id}


@app.delete("/api/admin/robots/{robot_id}")
async def admin_delete(robot_id: str):
    """Removes the registry row. Telemetry rows stay -- history is not rewritten."""
    reg = _need_registry()
    await reg.delete(robot_id)
    if writer:
        writer.note_event(robot_id, "revoke", {"action": "deleted"}, "warn")
    return {"ok": True, "id": robot_id}


@app.post("/api/enroll")
async def api_enroll(request: Request, body: Dict[str, Any]):
    """Redeem a one-time token for this robot's own MQTT credentials.

    Open to anyone holding a valid token -- that IS the credential. Rate limited
    on the same counter as failed logins so tokens cannot be brute forced.
    """
    reg = _need_registry()
    key = client_key(request)
    if rate_limited(key):
        raise HTTPException(429, "too many attempts")
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token is required")
    try:
        result = await reg.enroll(token, client_ip=key,
                                  hostname=str(body.get("hostname") or ""))
    except PermissionError as exc:
        note_failure(key)
        log.warning("enroll rejected from %s: %s", key, exc)
        raise HTTPException(403, str(exc))

    # Refresh now rather than waiting for the periodic tick: the agent connects
    # to MQTT immediately after enrolling, and being rejected on the first try
    # looks like a broken install.
    await reg.refresh_cache()

    if writer:
        writer.note_event(result["robot_id"], "enroll",
                          {"action": "enrolled", "ip": key,
                           "hostname": body.get("hostname")}, "info")
    mq = CFG["mqtt"]
    result["mqtt"] = {
        "host": mq.get("public_host") or request.url.hostname,
        "port": int(mq["port"]),
        "status_topic": f"fleet/{result['robot_id']}/status",
        "lwt_topic": f"fleet/{result['robot_id']}/lwt",
        "cmd_topic": f"fleet/{result['robot_id']}/cmd",
    }
    return result


ADMIN_PAGES = {"robots", "topics", "alerts", "events", "system"}


@app.get("/admin", response_class=HTMLResponse)
async def admin_root():
    return RedirectResponse("/admin/robots", status_code=302)


@app.get("/admin/{page}", response_class=HTMLResponse)
async def admin_page(page: str):
    """One file per section. The admin surface outgrew a single page, and a
    long scroll is where settings go to be missed."""
    if page not in ADMIN_PAGES:
        raise HTTPException(404, f"unknown admin page; try {sorted(ADMIN_PAGES)}")
    return FileResponse(WEB / f"admin_{page}.html")


@app.get("/agent/{filename}")
async def agent_download(filename: str):
    """Serve the agent wheel so a fresh Pi can `pip install` straight from here.

    Deliberately unauthenticated: the wheel carries no secrets, and requiring a
    password would mean putting one in the install command that gets pasted
    into shell history on every vehicle.
    """
    wheel = agent_wheel()
    if wheel is None:
        raise HTTPException(404, "agent wheel not built; run `pip wheel` in agent/")
    # Serve the alias directly rather than redirecting: pip decides how to treat
    # a download from the URL's extension, so a 302 from `/agent/latest` makes
    # it think the file is a source tree.
    if filename in ("latest", "latest.whl"):
        return FileResponse(wheel, media_type="application/octet-stream",
                            filename=wheel.name)
    if filename != wheel.name or "/" in filename or "\\" in filename:
        raise HTTPException(404, f"unknown file; current build is {wheel.name}")
    return FileResponse(wheel, media_type="application/octet-stream",
                        filename=wheel.name)


@app.get("/api/storage")
async def api_storage():
    """Measured on-disk footprint -- the honest answer to 'how big will this get'."""
    database = _need_db()
    stats = await database.storage_stats()
    per_row = stats.get("bytes_per_row")
    if per_row:
        # project from the actual observed ingest rate, not a guess
        rate = state.msg_count / max(time.time() - state.started, 1.0)
        stats["observed_rows_per_s"] = round(rate, 2)
        stats["projected_bytes_per_day"] = round(per_row * rate * 86400)
        stats["projected_gb_per_month"] = round(per_row * rate * 86400 * 30 / 1e9, 2)
        stats["projected_gb_at_retention"] = round(
            per_row * rate * 86400 * database.retention_days / 1e9, 2)
    stats["retention_days"] = database.retention_days
    return stats


@app.post("/api/command/{robot_id}")
async def api_command(robot_id: str, body: Dict[str, Any]):
    """Publish a command back down to one robot on `fleet/<id>/cmd`."""
    if broker is None:
        raise HTTPException(503, "commands require the embedded broker")
    topic = CFG["mqtt"]["command_topic"].format(id=robot_id)
    payload = json.dumps(body, separators=(",", ":")).encode()
    await broker.publish(topic, payload, qos=1, retain=False)
    return {"ok": True, "topic": topic, "bytes": len(payload)}


@app.get("/api/bwtest")
async def api_bwtest(bytes: int = 1_000_000):
    """Throughput probe: returns `bytes` of incompressible filler.

    Used by tools/bw_probe.py to find where Tailscale's undocumented Funnel
    bandwidth ceiling actually sits for your tailnet.
    """
    if not CFG["bwtest"]["enabled"]:
        raise HTTPException(404, "bwtest disabled")
    cap = int(CFG["bwtest"]["max_mb"]) * 1_000_000
    n = max(1, min(int(bytes), cap))
    blob = secrets.token_bytes(n)  # random -> defeats any transport compression
    return Response(blob, media_type="application/octet-stream",
                    headers={"cache-control": "no-store"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not ws_authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.join(ws)
    try:
        while True:
            # clients only ever send keepalive pings; ignore the content
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.leave(ws)


app.mount("/static", StaticFiles(directory=WEB), name="static")


def main() -> None:
    uvicorn.run(app, host=CFG["http"]["host"], port=int(CFG["http"]["port"]),
                log_level="warning",
                ws_ping_interval=float(CFG["dashboard"]["ws_ping_s"]),
                ws_ping_timeout=float(CFG["dashboard"]["ws_ping_s"]),
                access_log=False)


if __name__ == "__main__":
    main()
