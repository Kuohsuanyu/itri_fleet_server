"""Robot registry: enrollment tokens, per-robot MQTT credentials, revocation.

Enrollment is two-phase so no shared secret ever exists in the fleet:

  1. an operator creates a robot in the dashboard -> one-time token (15 min)
  2. the robot redeems the token over HTTPS -> receives its own random secret

The server stores only sha256(secret). That is deliberate rather than bcrypt:
the secret is 32 bytes of `secrets.token_urlsafe` entropy, so there is no
dictionary to attack and a slow KDF would only add latency to every broker
connection. Bcrypt is for *user-chosen* passwords; this is not one.

Credentials are cached in memory because the MQTT broker authenticates on the
event loop and must not wait on a database round trip per CONNECT.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

from .db import Database

log = logging.getLogger("registry")

# Unambiguous alphabet: no 0/O, 1/I/L -- these get read off a screen and typed.
TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def make_token(groups: int = 3, size: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(size))
        for _ in range(groups)
    )


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def slugify_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:63] or f"robot-{secrets.randbelow(9000) + 1000}"


class Registry:
    def __init__(self, db: Database, cache_ttl: float = 30.0):
        self.db = db
        self._cache: Dict[str, Tuple[Optional[str], bool]] = {}   # id -> (hash, revoked)
        self._cache_at = 0.0
        self._cache_ttl = cache_ttl

    # ------------------------------------------------------------------ read

    async def list_robots(self) -> List[Dict[str, Any]]:
        return await self.db.fetch("""
            SELECT r.id, r.name, r.tags, r.display, r.notes,
                   r.enrolled_at, r.revoked_at, r.last_seen,
                   (r.secret_hash IS NOT NULL) AS enrolled,
                   t.token AS pending_token, t.expires_at AS token_expires
            FROM robots r
            LEFT JOIN LATERAL (
                SELECT token, expires_at FROM enroll_tokens
                WHERE robot_id = r.id AND used_at IS NULL AND expires_at > now()
                ORDER BY created_at DESC LIMIT 1
            ) t ON true
            ORDER BY r.id
        """)

    async def get(self, robot_id: str) -> Optional[Dict[str, Any]]:
        rows = await self.db.fetch("SELECT * FROM robots WHERE id = %s", (robot_id,))
        return rows[0] if rows else None

    # ----------------------------------------------------------------- write

    async def create_robot(self, name: str, robot_id: Optional[str] = None,
                           tags: Optional[List[str]] = None,
                           ttl_minutes: int = 30) -> Dict[str, Any]:
        """Register a robot and mint its one-time enrollment token."""
        rid = (robot_id or slugify_id(name)).lower()
        if not ID_RE.match(rid):
            raise ValueError(f"invalid robot id {rid!r}: use a-z 0-9 - _ , 2-63 chars")
        if await self.get(rid):
            raise ValueError(f"robot {rid!r} already exists")

        await self.db.execute(
            "INSERT INTO robots (id, name, tags) VALUES (%s, %s, %s)",
            (rid, name or rid, tags or []))
        token = await self.issue_token(rid, name, ttl_minutes)
        self._invalidate()
        return {"id": rid, "name": name or rid, **token}

    async def issue_token(self, robot_id: str, name: Optional[str] = None,
                          ttl_minutes: int = 30) -> Dict[str, Any]:
        """Replace any outstanding token for this robot with a fresh one."""
        token = make_token()
        expires = datetime.now(timezone.utc) + timedelta(minutes=max(ttl_minutes, 1))
        await self.db.execute(
            "UPDATE enroll_tokens SET used_at = now()"
            " WHERE robot_id = %s AND used_at IS NULL", (robot_id,))
        await self.db.execute(
            "INSERT INTO enroll_tokens (token, robot_id, name, expires_at)"
            " VALUES (%s, %s, %s, %s)",
            (token, robot_id, name, expires))
        return {"token": token, "expires_at": expires, "ttl_minutes": ttl_minutes}

    async def enroll(self, token: str, client_ip: str = "",
                     hostname: str = "") -> Dict[str, Any]:
        """Redeem a token. Returns the plaintext secret exactly once."""
        rows = await self.db.fetch(
            "SELECT token, robot_id, expires_at, used_at FROM enroll_tokens"
            " WHERE token = %s", (token.strip().upper(),))
        if not rows:
            raise PermissionError("unknown token")
        row = rows[0]
        if row["used_at"] is not None:
            raise PermissionError("token already used")
        if row["expires_at"] < datetime.now(timezone.utc):
            raise PermissionError("token expired")

        rid = row["robot_id"]
        secret = secrets.token_urlsafe(32)
        await self.db.execute(
            "UPDATE robots SET secret_hash = %s, revoked_at = NULL,"
            " enrolled_at = now() WHERE id = %s",
            (hash_secret(secret), rid))
        await self.db.execute(
            "UPDATE enroll_tokens SET used_at = now(), used_by_ip = %s"
            " WHERE token = %s", (client_ip, row["token"]))
        self._invalidate()
        log.info("robot %r enrolled from %s (%s)", rid, client_ip, hostname or "?")
        return {"robot_id": rid, "mqtt_username": rid, "mqtt_password": secret}

    async def revoke(self, robot_id: str) -> None:
        """Kill the credential but keep the row -- history must stay attributable."""
        await self.db.execute(
            "UPDATE robots SET revoked_at = now(), secret_hash = NULL"
            " WHERE id = %s", (robot_id,))
        await self.db.execute(
            "UPDATE enroll_tokens SET used_at = now()"
            " WHERE robot_id = %s AND used_at IS NULL", (robot_id,))
        self._invalidate()
        log.warning("robot %r revoked", robot_id)

    async def update(self, robot_id: str, name: Optional[str] = None,
                     tags: Optional[List[str]] = None,
                     display: Optional[Dict[str, Any]] = None,
                     notes: Optional[str] = None) -> None:
        sets, params = [], []
        if name is not None:
            sets.append("name = %s"); params.append(name)
        if tags is not None:
            sets.append("tags = %s"); params.append(tags)
        if display is not None:
            sets.append("display = %s"); params.append(Jsonb(display))
        if notes is not None:
            sets.append("notes = %s"); params.append(notes)
        if not sets:
            return
        params.append(robot_id)
        await self.db.execute(
            f"UPDATE robots SET {', '.join(sets)} WHERE id = %s", params)

    async def delete(self, robot_id: str) -> None:
        await self.db.execute("DELETE FROM enroll_tokens WHERE robot_id = %s", (robot_id,))
        await self.db.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
        self._invalidate()

    # ----------------------------------------------------- broker credentials

    def _invalidate(self) -> None:
        self._cache_at = 0.0

    async def refresh_cache(self) -> None:
        rows = await self.db.fetch(
            "SELECT id, secret_hash, (revoked_at IS NOT NULL) AS revoked FROM robots")
        self._cache = {r["id"]: (r["secret_hash"], r["revoked"]) for r in rows}
        self._cache_at = time.monotonic()

    async def ensure_cache(self) -> None:
        if time.monotonic() - self._cache_at > self._cache_ttl:
            try:
                await self.refresh_cache()
            except Exception as exc:
                log.warning("credential cache refresh failed: %s", exc)

    def verify_cached(self, username: str, password: str) -> bool:
        """Synchronous check for the broker's CONNECT path.

        Reads only the in-memory cache: authenticating must never block on the
        database, and a DB outage must not lock the whole fleet out.
        """
        entry = self._cache.get(username)
        if entry is None:
            return False
        secret_hash, revoked = entry
        if revoked or not secret_hash:
            return False
        return secrets.compare_digest(hash_secret(password), secret_hash)

    def known_ids(self) -> List[str]:
        return sorted(self._cache)

    # Publish-only. There is no subscribe leaf at all: this system monitors and
    # does not command, so the downlink was removed rather than left unused.
    # An unused capability is still a capability -- anyone who took over the
    # server could have started sending on fleet/<id>/cmd, and every agent was
    # already subscribed and waiting. Deleting it means a compromised server
    # can corrupt records but cannot move a vehicle.
    PUBLISH_LEAVES = ("status", "lwt", "samples", "raw")

    @staticmethod
    def topic_allowed(username: str, topic: str, action: str) -> bool:
        """Each robot is confined to its own branch of the topic tree.

        Without this, an authenticated robot could publish as any other robot
        and the archive would stop being evidence. Subscription is denied
        outright, which also disposes of wildcard fishing (`fleet/+/status`).
        """
        if not username or action != "publish":
            return False
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "fleet" or parts[1] != username:
            return False
        # samples = batches of the vehicle's own topics, relayed by itri-agent.
        # raw is the pre-rename name, accepted so an older agent keeps working.
        return parts[2] in Registry.PUBLISH_LEAVES
