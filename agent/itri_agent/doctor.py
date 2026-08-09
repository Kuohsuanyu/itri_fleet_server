"""環境掃描:看清楚這台機器長什麼樣,然後建議怎麼裝。

**這支不會改動任何東西。** 純讀取 —— 不裝套件、不改設定、不動 ROS、
不碰系統 Python。它只回答「你這台機器該用哪種方式跑 agent」,
並且明確列出建議的做法會改動什麼、不會碰什麼。

    itri-agent doctor            掃描並給建議
    itri-agent doctor --json     機器可讀,給腳本用
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

B, D, R = "\x1b[1m", "\x1b[2m", "\x1b[0m"
GRN, RED, YEL, CYA = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[36m"


def _ok(msg: str, detail: str = "") -> None:
    print(f"  {GRN}OK{R}   {msg}" + (f"  {D}{detail}{R}" if detail else ""))


def _warn(msg: str, detail: str = "") -> None:
    print(f"  {YEL}!{R}    {msg}" + (f"  {D}{detail}{R}" if detail else ""))


def _bad(msg: str, detail: str = "") -> None:
    print(f"  {RED}X{R}    {msg}" + (f"  {D}{detail}{R}" if detail else ""))


def _info(msg: str) -> None:
    print(f"  {D}·    {msg}{R}")


# --------------------------------------------------------------- detection

def check_python() -> Dict[str, Any]:
    v = sys.version_info
    in_venv = sys.prefix != sys.base_prefix
    # a venv created with --system-site-packages leaves this file absent/false
    system_site = True
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    if in_venv and cfg.exists():
        text = cfg.read_text(encoding="utf-8", errors="replace")
        system_site = "include-system-site-packages = true" in text.lower()
    return {
        "version": f"{v.major}.{v.minor}.{v.micro}",
        "ok": v >= (3, 9),
        "executable": sys.executable,
        "in_venv": in_venv,
        "system_site_packages": system_site if in_venv else True,
        "pipx": "pipx" in sys.executable.replace("\\", "/"),
    }


def check_pep668() -> Dict[str, Any]:
    """Debian 12 / Raspberry Pi OS Bookworm 之後,系統 Python 拒絕 pip 安裝。"""
    marker = Path(getattr(sys, "base_prefix", sys.prefix)) / "lib"
    found = None
    for p in marker.glob("python3*/EXTERNALLY-MANAGED"):
        found = str(p)
        break
    if not found:
        for p in Path("/usr/lib").glob("python3*/EXTERNALLY-MANAGED"):
            found = str(p)
            break
    return {"externally_managed": bool(found), "marker": found}


def check_ros() -> Dict[str, Any]:
    distro = os.environ.get("ROS_DISTRO")
    has_rclpy = importlib.util.find_spec("rclpy") is not None
    setup_files = sorted(str(p) for p in Path("/opt/ros").glob("*/setup.bash")) \
        if Path("/opt/ros").exists() else []
    installed = [Path(p).parent.name for p in setup_files]
    topics: List[Tuple[str, str]] = []
    err = None
    if has_rclpy:
        topics, err = _list_ros_topics()
    return {
        "installed_distros": installed,
        "ROS_DISTRO": distro,
        "rclpy_importable": has_rclpy,
        "topics": topics,
        "topic_error": err,
    }


def _list_ros_topics(timeout: float = 6.0):
    """短暫起一個 node 列出 topic,列完立刻關掉。純讀取,不訂閱。"""
    try:
        import rclpy
        from rclpy.node import Node
    except Exception as exc:
        return [], f"import 失敗: {exc}"
    started_here = False
    try:
        if not rclpy.ok():
            rclpy.init(args=None)
            started_here = True
        node = Node("itri_agent_doctor")
        deadline = time.time() + timeout
        found: List[Tuple[str, str]] = []
        while time.time() < deadline:
            found = [(t, ty[0] if ty else "?")
                     for t, ty in node.get_topic_names_and_types()]
            if len(found) > 2:          # /rosout 和 /parameter_events 一定有
                break
            time.sleep(0.4)
        node.destroy_node()
        return sorted(found), None
    except Exception as exc:
        return [], str(exc)
    finally:
        if started_here:
            try:
                import rclpy
                rclpy.shutdown()
            except Exception:
                pass


def check_mqtt_broker(hosts=("127.0.0.1", "localhost"), port: int = 1883) -> Dict[str, Any]:
    for h in hosts:
        with socket.socket() as s:
            s.settimeout(0.6)
            if s.connect_ex((h, port)) == 0:
                return {"found": True, "host": h, "port": port}
    return {"found": False, "host": None, "port": port}


def check_tailscale() -> Dict[str, Any]:
    exe = shutil.which("tailscale") or "/usr/bin/tailscale"
    if not Path(exe).exists():
        return {"installed": False}
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=15).stdout
        d = json.loads(out)
        self_ = d.get("Self") or {}
        return {
            "installed": True,
            "logged_in": bool(self_.get("DNSName")),
            "name": (self_.get("DNSName") or "").rstrip("."),
            "tags": self_.get("Tags") or [],
            "peers": len(d.get("Peer") or {}),
        }
    except Exception as exc:
        return {"installed": True, "logged_in": False, "error": str(exc)}


def check_server(cred: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not cred:
        return {"enrolled": False}
    mq = cred.get("mqtt") or {}
    host, port = mq.get("host"), int(mq.get("port", 1883))
    reach = False
    if host:
        with socket.socket() as s:
            s.settimeout(3.0)
            reach = s.connect_ex((host, port)) == 0
    api = None
    if cred.get("server"):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(cred["server"] + "/healthz", timeout=8) as r:
                api = r.status
        except Exception:
            api = None
    return {"enrolled": True, "robot_id": cred.get("robot_id"),
            "broker": f"{host}:{port}", "broker_reachable": reach,
            "server": cred.get("server"), "api_status": api}


def check_paho() -> Dict[str, Any]:
    try:
        import paho.mqtt as p
        return {"installed": True, "version": getattr(p, "__version__", "?"),
                "path": p.__file__}
    except ImportError:
        return {"installed": False}


# ------------------------------------------------------------ recommendation

def recommend(py, pep, ros, broker) -> Dict[str, Any]:
    """挑一種安裝/執行方式,並說清楚它會動到什麼。"""
    ros_usable = ros["rclpy_importable"]
    ros_present = bool(ros["installed_distros"]) or bool(ros["ROS_DISTRO"])

    if ros_usable:
        source = "ros2"
    elif ros_present and not ros_usable:
        source = "ros2-blocked"
    elif broker["found"]:
        source = "mqtt"
    else:
        source = "none"

    distro = ros["ROS_DISTRO"] or (ros["installed_distros"] or ["humble"])[0]

    if source == "ros2":
        return {
            "source": "ros2",
            "verdict": "ROS 2 可用,直接用 ROS 模式",
            "commands": ["itri-agent --source ros2"],
            "changes": [],
            "untouched": ["系統 Python", "ROS 安裝", "現有套件版本"],
            "why": (f"這個 shell 已經 source 過 setup.bash 所以 rclpy 找得到。"
                    f"注意 systemd 不會繼承這個環境 —— install-service "
                    f"產生的 unit 會自己 source /opt/ros/{distro}/setup.bash。"),
        }

    if source == "ros2-blocked":
        return {
            "source": "ros2",
            "verdict": f"偵測到 ROS 2({distro})但這個 Python 看不到 rclpy",
            "commands": [
                f"source /opt/ros/{distro}/setup.bash",
                "python3 -m venv --system-site-packages ~/.itri-venv",
                "~/.itri-venv/bin/pip install <wheel 網址>",
                "# 之後每次執行都要先 source(見下方說明)",
                f"source /opt/ros/{distro}/setup.bash && "
                f"~/.itri-venv/bin/itri-agent --source ros2",
            ],
            "changes": [
                "新建 ~/.itri-venv 這個資料夾",
                "在那個 venv 內安裝 itri-fleet-agent 與 paho-mqtt",
            ],
            "untouched": [
                "系統 Python 的套件(venv 只會「看到」不會「改」它們)",
                "ROS 2 安裝與 /opt/ros",
                "既有的 paho-mqtt(venv 內的版本只在 agent 行程內生效)",
            ],
            "why": (
                "apt 裝的 rclpy 在 /opt/ros/%s/lib/pythonX/site-packages,"
                "那是 setup.bash 靠 PYTHONPATH 掛上去的,不在系統 site-packages 裡;"
                "它的 .so 也要 setup.bash 設的 LD_LIBRARY_PATH 才載得到。"
                "所以 source 是「每次執行都要」,不是裝一次就好 —— "
                "把它加進 ~/.bashrc,或直接用 install-service"
                "(產生的 unit 會自己 source)。"
                "--system-site-packages 則讓 venv 另外看得到 apt 裝的其他相依,"
                "而 pip 裝的東西仍然只留在 venv 裡。" % distro),
        }

    if source == "mqtt":
        cmds = (["pipx install <wheel 網址>", "itri-agent"]
                if not pep["externally_managed"] or shutil.which("pipx")
                else ["sudo apt install -y pipx && pipx ensurepath",
                      "pipx install <wheel 網址>", "itri-agent"])
        return {
            "source": "mqtt",
            "verdict": f"沒有 ROS 2,但本地有 MQTT broker({broker['host']}:{broker['port']})",
            "commands": cmds,
            "changes": ["pipx 建立一個獨立環境安裝 agent",
                        "(若尚未安裝)apt 安裝 pipx"],
            "untouched": ["系統 Python 的套件", "現有套件版本"],
        }

    return {
        "source": "none",
        "verdict": "既沒有可用的 ROS 2,也找不到本地 MQTT broker",
        "commands": [],
        "changes": [],
        "untouched": [],
        "why": ("agent 需要一個資料來源。確認底盤程式有在跑,"
                "或用 --host 指定 broker 位址(它可能不在 127.0.0.1)。"),
    }


# ----------------------------------------------------------------- reporting

def run(as_json: bool = False) -> int:
    from . import config as cfgmod

    py = check_python()
    pep = check_pep668()
    ros = check_ros()
    broker = check_mqtt_broker()
    ts = check_tailscale()
    paho = check_paho()
    cred = cfgmod.load_credentials()
    srv = check_server(cred)
    rec = recommend(py, pep, ros, broker)

    if as_json:
        print(json.dumps({"python": py, "pep668": pep, "ros2": ros,
                          "mqtt_broker": broker, "tailscale": ts, "paho": paho,
                          "server": srv, "recommendation": rec},
                         indent=2, ensure_ascii=False, default=str))
        return 0 if rec["source"] != "none" else 1

    print(f"\n{B}{CYA}  itri-agent 環境掃描{R}")
    print(f"{D}  這支不會改動任何東西 —— 純讀取。{R}")
    print(f"{D}  {platform.platform()}{R}")

    print(f"\n{B}Python{R}")
    (_ok if py["ok"] else _bad)(f"Python {py['version']}",
                                "需要 3.9 以上" if not py["ok"] else "")
    _info(f"直譯器 {py['executable']}")
    if py["in_venv"]:
        if py["system_site_packages"]:
            _ok("在 venv 內,且看得到系統套件", "ROS 模式需要這個")
        else:
            _warn("在隔離的 venv/pipx 內,看不到系統套件",
                  "有 ROS 的話會 import 不到 rclpy")
    else:
        _info("使用系統 Python")
    if pep["externally_managed"]:
        _warn("系統 Python 標記為 externally-managed(PEP 668)",
              "pip 不能直接裝進系統 Python")

    print(f"\n{B}ROS 2{R}")
    if ros["installed_distros"]:
        _ok(f"已安裝:{', '.join(ros['installed_distros'])}",
            f"ROS_DISTRO={ros['ROS_DISTRO'] or '(未 source)'}")
    else:
        _info("這台機器上找不到 /opt/ros")
    if ros["rclpy_importable"]:
        _ok("rclpy 可以 import")
        if ros["topic_error"]:
            _warn(f"列出 topic 時出錯:{ros['topic_error']}")
        else:
            real = [t for t, _ in ros["topics"]
                    if t not in ("/rosout", "/parameter_events")]
            if real:
                _ok(f"ROS 圖譜上有 {len(real)} 個 topic")
                for t, ty in ros["topics"][:8]:
                    print(f"       {t:<34} {D}{ty}{R}")
                if len(ros["topics"]) > 8:
                    print(f"       {D}… 還有 {len(ros['topics']) - 8} 個{R}")
            else:
                _warn("ROS 有跑但圖譜上沒有實際 topic", "底盤程式啟動了嗎?")
    elif ros["installed_distros"]:
        _bad("這個 Python 看不到 rclpy", "見下方建議")

    print(f"\n{B}本地 MQTT broker{R}")
    if broker["found"]:
        _ok(f"{broker['host']}:{broker['port']} 有在聽")
    else:
        _info("127.0.0.1:1883 沒有 broker")

    print(f"\n{B}Tailscale{R}")
    if not ts.get("installed"):
        _bad("未安裝", "車子要靠它連到伺服器的 1883")
    elif ts.get("logged_in"):
        _ok(f"已登入:{ts['name']}",
            f"tags={ts['tags'] or '(無 —— 應該要有 tag:robot)'}  peers={ts['peers']}")
        if not ts.get("tags"):
            _warn("這台沒有 tag", "user device 的金鑰 180 天會過期,車子建議用 tag:robot")
    else:
        _bad("已安裝但未登入", "tailscale up --authkey=...")

    print(f"\n{B}與伺服器的連線{R}")
    if not srv["enrolled"]:
        _info("尚未登記 —— 跑 itri-agent 進設定精靈")
    else:
        _ok(f"已登記為 {srv['robot_id']}")
        (_ok if srv["broker_reachable"] else _bad)(
            f"broker {srv['broker']}",
            "" if srv["broker_reachable"] else "連不上 —— 檢查 Tailscale 與 ACL")
        if srv["api_status"] == 200:
            _ok(f"API {srv['server']}")
        else:
            _warn(f"API {srv['server']} 無回應", "登記可以,但重新登記時會失敗")

    print(f"\n{B}{CYA}建議{R}")
    print(f"  {B}{rec['verdict']}{R}")
    if rec.get("why"):
        print(f"  {D}{rec['why']}{R}")
    if rec["commands"]:
        print()
        for c in rec["commands"]:
            print(f"    {c}")
    if rec["changes"]:
        print(f"\n  {YEL}會改動:{R}")
        for c in rec["changes"]:
            print(f"    · {c}")
    if rec["untouched"]:
        print(f"\n  {GRN}不會碰:{R}")
        for c in rec["untouched"]:
            print(f"    · {c}")
    print()
    return 0 if rec["source"] != "none" else 1
