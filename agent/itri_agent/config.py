"""Agent configuration and credential storage.

Two files, kept apart on purpose:

  ~/.itri-fleet/config.json       what to relay -- safe to copy between vehicles
  ~/.itri-fleet/credentials.json  this vehicle's own MQTT secret, mode 0600

Standard library only. This runs on a Raspberry Pi where every extra dependency
is another thing that can fail to build.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(os.environ.get("ITRI_AGENT_HOME", Path.home() / ".itri-fleet"))
CONFIG_PATH = HOME / "config.json"
CRED_PATH = HOME / "credentials.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    # the vehicle's own broker -- whatever the chassis already publishes to
    "local": {"host": "127.0.0.1", "port": 1883, "username": None, "password": None},

    # Topics to relay. Empty list = relay everything matched by `subscribe`.
    # `itri-agent discover` writes this for you after you pick from the list.
    "subscribe": ["#"],
    "include": [],
    "exclude": [
        "fleet/#",          # never mirror our own uplink back into itself
        "$SYS/#",
    ],

    # Rate control. Relaying a 50 Hz topic at 50 Hz is almost never wanted, and
    # the cost lands on the server's disk, not this Pi.
    "max_rate_hz": 5.0,         # per topic ceiling
    "on_change_only": True,     # skip repeats of an identical value
    "deadband": 0.0,            # numeric: skip if |new-old| <= this
    "max_payload_bytes": 8192,  # anything larger is dropped and counted

    "publish_hz": 1.0,          # uplink batch rate
    "max_batch": 500,
    "buffer_max": 200000,       # samples held while the uplink is down

    # Optional: light up the dashboard's standard cards. Purely cosmetic --
    # everything is archived under its original topic either way.
    "map": {},                  # e.g. {"battery": "chassis/bat_pct"}
}


def _ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy of the defaults
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def save_config(cfg: Dict[str, Any]) -> Path:
    _ensure_home()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    return CONFIG_PATH


def load_credentials() -> Optional[Dict[str, Any]]:
    if not CRED_PATH.exists():
        return None
    return json.loads(CRED_PATH.read_text(encoding="utf-8"))


def save_credentials(cred: Dict[str, Any]) -> Path:
    _ensure_home()
    CRED_PATH.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    try:
        os.chmod(CRED_PATH, stat.S_IRUSR | stat.S_IWUSR)   # 0600; no-op on Windows
    except OSError:
        pass
    return CRED_PATH


def topic_matches(filt: str, topic: str) -> bool:
    """MQTT wildcard match, same rules as the broker."""
    f, t = filt.split("/"), topic.split("/")
    for i, seg in enumerate(f):
        if seg == "#":
            return i == len(f) - 1
        if i >= len(t):
            return False
        if seg == "+":
            continue
        if seg != t[i]:
            return False
    return len(f) == len(t)


def should_relay(topic: str, include: List[str], exclude: List[str]) -> bool:
    """Exclude wins over include; an empty include list means 'everything'."""
    if any(topic_matches(f, topic) for f in exclude):
        return False
    if not include:
        return True
    return any(topic_matches(f, topic) for f in include)
