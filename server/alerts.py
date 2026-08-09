"""Alert engine and notification channels.

Rules are evaluated against the in-memory fleet state on every pusher tick, so
detection latency is the tick period (1 s) and never depends on the database.
Rules themselves live in Postgres and are edited in the dashboard, so changing a
threshold does not need a restart.

Three guards stop alerting from becoming noise, which is the usual reason people
switch it off:

  for_seconds  the condition must hold this long before firing
  clear_value  hysteresis, so a value resting on the threshold cannot flap
  cooldown_min a firing rule re-notifies at most this often
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pywebpush import WebPushException, webpush

from . import state as state_mod

log = logging.getLogger("alerts")

OPS_NUMERIC = {"lt", "gt", "outside", "inside"}


@dataclass
class RuleState:
    """Per (rule, robot) state machine: ok -> pending -> firing -> ok."""
    pending_since: float = 0.0
    firing_since: float = 0.0
    last_notified: float = 0.0
    alert_id: Optional[int] = None


class AlertEngine:
    def __init__(self, state, db, writer, channels: Dict[str, Any]):
        self.state = state
        self.db = db
        self.writer = writer
        self.channels = channels or {}
        self.rules: List[Dict[str, Any]] = []
        self._rules_at = 0.0
        self._states: Dict[Tuple[int, str], RuleState] = {}
        # latest relayed topic value per robot, for topic-sourced rules
        self.topic_values: Dict[str, Dict[str, Any]] = {}
        self.topic_seen: Dict[str, Dict[str, float]] = {}
        self.fired = 0
        self.resolved = 0
        self.notify_failures = 0
        self.last_error: Optional[str] = None

    # ---------------------------------------------------------------- inputs

    def observe_topics(self, robot_id: str, batch: list) -> None:
        """Called with every relayed batch so topic rules see live values."""
        vals = self.topic_values.setdefault(robot_id, {})
        seen = self.topic_seen.setdefault(robot_id, {})
        now = time.time()
        for item in batch:
            try:
                vals[item[0]] = item[2]
                seen[item[0]] = now
            except (TypeError, IndexError):
                continue

    async def refresh_rules(self, force: bool = False) -> None:
        if not self.db or not self.db.ready:
            return
        if not force and time.time() - self._rules_at < 10:
            return
        try:
            self.rules = await self.db.fetch(
                "SELECT * FROM alert_rules WHERE enabled ORDER BY id")
            self._rules_at = time.time()
        except Exception as exc:
            log.warning("could not load alert rules: %s", exc)

    # ------------------------------------------------------------ evaluation

    def _current(self, robot, rule) -> Tuple[Optional[float], Any]:
        """(numeric, raw) for this rule's subject, or (None, None) if absent."""
        src, key = rule["source"], rule["key"]
        if src == "presence":
            return (0.0 if robot.online else 1.0), robot.presence
        if src == "field":
            raw = {
                "battery": robot.battery, "state": robot.state,
                "v": robot.v, "w": robot.w, "temp": robot.temp,
                "wifi": robot.wifi, "odom": robot.odom,
                "errors": len(robot.errors),
                "age": time.time() - robot.last_seen if robot.last_seen else None,
            }.get(key, robot.extra.get(key))
        else:
            raw = self.topic_values.get(robot.id, {}).get(key)
        if isinstance(raw, bool):
            return (1.0 if raw else 0.0), raw
        if isinstance(raw, (int, float)):
            return float(raw), raw
        return None, raw

    def _triggered(self, rule, num: Optional[float], raw: Any,
                   robot, clearing: bool) -> bool:
        op = rule["op"]
        if op == "offline":
            # Fires for anything that is not demonstrably OK, including STALE
            # and UNKNOWN. "Not reporting" is the condition worth waking up
            # for; which flavour of not-reporting is detail for the message.
            age = time.time() - robot.last_seen if robot.last_seen else 1e9
            return robot.presence != state_mod.OK or age > float(rule["value"] or 60)
        if op == "stale":
            # Narrower and more interesting: the link looks fine but the data
            # stopped. This is the failure that a boolean online/offline model
            # cannot express at all.
            return robot.presence == state_mod.STALE
        if op == "eq":
            return str(raw) == str(rule["text_value"])
        if op == "ne":
            return str(raw) != str(rule["text_value"])
        if num is None:
            return False

        # hysteresis: while firing, compare against clear_value instead
        limit = rule["value"]
        if clearing and rule["clear_value"] is not None:
            limit = rule["clear_value"]
        if op == "lt":
            return num < float(limit)
        if op == "gt":
            return num > float(limit)
        if op in ("outside", "inside"):
            lo, hi = float(rule["value"]), float(rule["value2"] or rule["value"])
            within = lo <= num <= hi
            return (not within) if op == "outside" else within
        return False

    def _message(self, rule, robot, num, raw) -> str:
        subject = rule["key"] or "狀態"
        shown = raw if num is None else (round(num, 3) if isinstance(num, float) else num)
        op = rule["op"]

        template = (rule.get("message_template") or "").strip()
        if template:
            try:
                return template.format(
                    robot=robot.name, id=robot.id, key=subject, value=shown,
                    limit=rule["value"], limit2=rule["value2"],
                    rule=rule["name"], severity=rule["severity"])
            except (KeyError, IndexError, ValueError) as exc:
                # A bad placeholder must not silence the alert
                log.warning("rule %r has a broken message template (%s)",
                            rule["name"], exc)

        if op == "offline":
            label = {state_mod.STALE: "資料停止(連線仍在)",
                     state_mod.UNKNOWN: "從未回報",
                     state_mod.OFFLINE: "離線"}.get(robot.presence, "離線")
            return f"{robot.name} {label}"
        if op == "stale":
            age = int(time.time() - robot.last_seen) if robot.last_seen else 0
            return f"{robot.name} 連線仍在但已 {age} 秒沒有資料"
        if op == "lt":
            return f"{robot.name} {subject}={shown} 低於 {rule['value']}"
        if op == "gt":
            return f"{robot.name} {subject}={shown} 高於 {rule['value']}"
        if op == "outside":
            return f"{robot.name} {subject}={shown} 超出範圍 {rule['value']}~{rule['value2']}"
        if op == "inside":
            return f"{robot.name} {subject}={shown} 落在範圍 {rule['value']}~{rule['value2']}"
        return f"{robot.name} {subject}={shown} ({op} {rule.get('text_value')})"

    async def tick(self) -> None:
        await self.refresh_rules()
        if not self.rules:
            return
        now = time.time()

        for rule in self.rules:
            scope = rule["robot_id"]
            robots = ([self.state.robots[scope]] if scope in self.state.robots
                      else [] if scope else list(self.state.robots.values()))
            for robot in robots:
                key = (rule["id"], robot.id)
                st = self._states.setdefault(key, RuleState())
                num, raw = self._current(robot, rule)
                firing = st.firing_since > 0
                hit = self._triggered(rule, num, raw, robot, clearing=firing)

                if hit and not firing:
                    if not st.pending_since:
                        st.pending_since = now
                    elif now - st.pending_since >= float(rule["for_seconds"]):
                        st.firing_since = now
                        st.pending_since = 0.0
                        await self._fire(rule, robot, num, raw, st)
                elif hit and firing:
                    cooldown = float(rule["cooldown_min"]) * 60
                    if cooldown > 0 and now - st.last_notified >= cooldown:
                        await self._notify(rule, self._message(rule, robot, num, raw)
                                           + "(持續中)", robot, repeat=True)
                        st.last_notified = now
                elif not hit:
                    st.pending_since = 0.0
                    if firing:
                        st.firing_since = 0.0
                        await self._resolve(rule, robot, st)

    # --------------------------------------------------------------- actions

    async def _fire(self, rule, robot, num, raw, st: RuleState) -> None:
        msg = self._message(rule, robot, num, raw)
        self.fired += 1
        st.last_notified = time.time()
        log.warning("ALERT %s: %s", rule["severity"], msg)
        if self.writer:
            self.writer.note_event(robot.id, "alert",
                                   {"rule": rule["name"], "message": msg},
                                   rule["severity"])
        sent = await self._notify(rule, msg, robot)
        if self.db and self.db.ready:
            try:
                rows = await self.db.fetch(
                    "INSERT INTO alerts (rule_id, rule_name, robot_id, severity,"
                    " message, value, notified) VALUES (%s,%s,%s,%s,%s,%s,%s)"
                    " RETURNING id",
                    (rule["id"], rule["name"], robot.id, rule["severity"],
                     msg, num, sent))
                st.alert_id = rows[0]["id"] if rows else None
            except Exception as exc:
                log.warning("could not record alert: %s", exc)

    async def _resolve(self, rule, robot, st: RuleState) -> None:
        self.resolved += 1
        msg = f"{robot.name} {rule['name']} 已恢復"
        log.info("RESOLVED: %s", msg)
        if self.writer:
            self.writer.note_event(robot.id, "alert",
                                   {"rule": rule["name"], "resolved": True}, "info")
        if st.alert_id and self.db and self.db.ready:
            try:
                await self.db.execute(
                    "UPDATE alerts SET resolved_at = now() WHERE id = %s",
                    (st.alert_id,))
            except Exception:
                pass
        st.alert_id = None
        if rule["severity"] == "critical":
            await self._notify(rule, "✅ " + msg, robot, resolved=True)

    def open_alerts(self) -> List[Dict[str, Any]]:
        out = []
        for (rule_id, robot_id), st in self._states.items():
            if st.firing_since:
                rule = next((r for r in self.rules if r["id"] == rule_id), None)
                robot = self.state.robots.get(robot_id)
                if rule and robot:
                    num, raw = self._current(robot, rule)
                    out.append({
                        "rule_id": rule_id, "rule": rule["name"],
                        "robot_id": robot_id, "robot": robot.name,
                        "severity": rule["severity"],
                        "message": self._message(rule, robot, num, raw),
                        "since": round(st.firing_since, 1),
                        "for_s": round(time.time() - st.firing_since, 1),
                    })
        out.sort(key=lambda a: (a["severity"] != "critical", a["since"]))
        return out

    # -------------------------------------------------------------- channels

    async def _notify(self, rule, message: str, robot, repeat: bool = False,
                      resolved: bool = False) -> List[str]:
        wanted = rule.get("channels") or list(self.channels)
        title = f"[{rule['severity'].upper()}] {rule['name']}"
        sent: List[str] = []
        for name in wanted:
            conf = self.channels.get(name)
            if not conf or not conf.get("enabled"):
                continue
            try:
                await send_to(name, conf, title, message, rule["severity"],
                              resolved, db=self.db)
                sent.append(name)
            except Exception as exc:
                self.notify_failures += 1
                self.last_error = f"{name}: {type(exc).__name__}: {exc}"
                log.warning("notify via %s failed: %s", name, exc)
        return sent


# --------------------------------------------------------------------- senders

async def send_to(name: str, conf: Dict[str, Any], title: str, message: str,
                  severity: str, resolved: bool = False, db=None) -> None:
    if name == "push":
        await _webpush(conf, title, message, severity, resolved, db)
    elif name == "ntfy":
        await _ntfy(conf, title, message, severity, resolved)
    elif name == "telegram":
        await _telegram(conf, title, message)
    elif name == "webhook":
        await _webhook(conf, title, message, severity, resolved)
    elif name == "line":
        await _line(conf, title, message)
    elif name == "email":
        await asyncio.to_thread(_email, conf, title, message)
    else:
        raise ValueError(f"unknown channel {name!r}")


async def _webpush(conf, title, message, severity, resolved, db) -> None:
    """Web Push to every subscribed browser / installed PWA.

    Runs in a thread: pywebpush is synchronous and does ECDH + AES-GCM per
    subscription, which is CPU work that must not sit on the event loop.
    Subscriptions the push service rejects with 404/410 are gone for good and
    get deleted rather than retried forever.
    """
    if db is None or not db.ready:
        raise RuntimeError("push needs the database to look up subscriptions")
    subs = await db.fetch("SELECT endpoint, p256dh, auth FROM push_subscriptions")
    if not subs:
        raise RuntimeError("no devices subscribed to push yet")

    payload = json.dumps({
        "title": title, "body": message, "severity": severity,
        "resolved": resolved, "ts": time.time(),
    }, ensure_ascii=False)

    dead, ok = [], 0
    for sub in subs:
        info = {"endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            await asyncio.to_thread(
                webpush, subscription_info=info, data=payload,
                vapid_private_key=conf["private_key"],
                vapid_claims={"sub": conf.get("contact", "mailto:admin@example.com")},
                ttl=int(conf.get("ttl", 3600)))
            ok += 1
        except WebPushException as exc:
            code = getattr(exc.response, "status_code", None)
            if code in (404, 410):
                dead.append(sub["endpoint"])
            else:
                log.warning("push to %s failed: %s", sub["endpoint"][:40], exc)
        except Exception as exc:
            log.warning("push error: %s", exc)

    if dead:
        await db.execute("DELETE FROM push_subscriptions WHERE endpoint = ANY(%s)",
                         (dead,))
        log.info("removed %d expired push subscriptions", len(dead))
    if ok:
        await db.execute("UPDATE push_subscriptions SET last_ok = now()"
                         " WHERE endpoint <> ALL(%s)", (dead,))
    else:
        raise RuntimeError(f"no push delivered ({len(subs)} subs, {len(dead)} expired)")


async def _ntfy(conf, title, message, severity, resolved) -> None:
    url = conf["url"].rstrip("/") + "/" + conf["topic"]
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", "replace"),
        "Priority": "3" if resolved else ("5" if severity == "critical" else "4"),
        "Tags": "white_check_mark" if resolved else (
            "rotating_light" if severity == "critical" else "warning"),
    }
    if conf.get("token"):
        headers["Authorization"] = f"Bearer {conf['token']}"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, content=message.encode("utf-8"), headers=headers)
        r.raise_for_status()


async def _telegram(conf, title, message) -> None:
    url = f"https://api.telegram.org/bot{conf['bot_token']}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json={"chat_id": conf["chat_id"],
                                    "text": f"{title}\n{message}"})
        r.raise_for_status()


async def _webhook(conf, title, message, severity, resolved) -> None:
    payload = {"title": title, "message": message, "severity": severity,
               "resolved": resolved, "ts": time.time()}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(conf["url"], json=payload,
                         headers=conf.get("headers") or {})
        r.raise_for_status()


async def _line(conf, title, message) -> None:
    """LINE Messaging API. Note: LINE Notify shut down on 2025-03-31, and the
    replacement only allows 200 free pushes a month -- reserve it for critical."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post("https://api.line.me/v2/bot/message/push",
                         headers={"Authorization": f"Bearer {conf['access_token']}"},
                         json={"to": conf["to"],
                               "messages": [{"type": "text",
                                             "text": f"{title}\n{message}"}]})
        r.raise_for_status()


def _email(conf, title, message) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = conf["from"]
    msg["To"] = ", ".join(conf["to"]) if isinstance(conf["to"], list) else conf["to"]
    msg.set_content(message)
    port = int(conf.get("port", 587))
    with smtplib.SMTP(conf["host"], port, timeout=20) as s:
        if conf.get("starttls", True):
            s.starttls()
        if conf.get("username"):
            s.login(conf["username"], conf["password"])
        s.send_message(msg)
