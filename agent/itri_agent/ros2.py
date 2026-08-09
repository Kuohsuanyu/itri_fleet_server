"""ROS 2 來源:動態發現所有 topic 並訂閱,不需要事先知道底盤發什麼。

跟 MQTT 來源一樣的原則 —— **不認識任何欄位**。訂閱全部、把訊息攤平成
`topic/欄位/子欄位 = 純量`,原樣往上送,由伺服器決定怎麼顯示。

三個 ROS 2 特有的坑,這裡都處理了:

1. **QoS 不匹配會靜默收不到訊息。** 大部分感測器驅動用 BEST_EFFORT 發佈,
   而預設訂閱是 RELIABLE —— 訂了但一筆都收不到,還不會報錯。這裡會先查
   發佈端的 QoS 再照它訂。

2. **訊息是結構,不是純量。** `/odom` 裡有幾十個欄位。攤平成
   `/odom/twist/twist/linear/x` 這種路徑,每個純量一筆。

3. **陣列會爆炸。** `/scan` 的 ranges 有幾百個 float,全部攤平會瞬間灌爆
   資料庫。超過 `max_array` 的陣列直接跳過,只留長度。

安裝注意:`rclpy` 是 apt 裝的系統套件,pipx/venv 的隔離環境看不到它。
用 ROS 2 模式時必須用 `--system-site-packages` 建環境,見 README。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("ros2")

# 這些型別攤平之後沒有意義,或大到不該轉發
SKIP_TYPES = (
    "sensor_msgs/msg/Image",
    "sensor_msgs/msg/CompressedImage",
    "sensor_msgs/msg/PointCloud2",
    "sensor_msgs/msg/LaserScan",       # ranges 陣列太大;要的話自己從 include 加回來
    "nav_msgs/msg/OccupancyGrid",
    "sensor_msgs/msg/PointCloud",
    "tf2_msgs/msg/TFMessage",          # 高頻且欄位極多
)

SKIP_TOPICS = ("/rosout", "/parameter_events", "/clock")


def available() -> Tuple[bool, str]:
    """(能不能用, 原因)。呼叫端用這個決定要不要提示使用者。"""
    try:
        import rclpy  # noqa: F401
    except ImportError as exc:
        return False, (
            f"找不到 rclpy({exc})。ROS 2 的 Python 套件是 apt 裝的系統套件,"
            "pipx/venv 的隔離環境看不到它。\n"
            "    先 source /opt/ros/<distro>/setup.bash,並用 "
            "--system-site-packages 建立環境。")
    return True, "rclpy 可用"


def _probe_graph(timeout: float = 4.0) -> bool:
    """Is there anything on the ROS graph besides the two built-in topics?

    Used by `source: auto`. A machine with ROS installed but nothing running
    should fall back to MQTT rather than sitting on an empty graph.
    """
    try:
        import rclpy
        from rclpy.node import Node
    except Exception:
        return False
    started = False
    try:
        if not rclpy.ok():
            rclpy.init(args=None)
            started = True
        node = Node("itri_agent_probe")
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                names = [t for t, _ in node.get_topic_names_and_types()
                         if t not in SKIP_TOPICS]
                if names:
                    return True
                time.sleep(0.3)
            return False
        finally:
            node.destroy_node()
    except Exception:
        return False
    finally:
        if started:
            try:
                import rclpy
                rclpy.shutdown()
            except Exception:
                pass


def flatten(prefix: str, value: Any, out: Dict[str, Any],
            max_array: int = 8, depth: int = 0) -> None:
    """把 ROS 訊息攤平成 {路徑: 純量}。

    深度和陣列長度都設上限 —— 一個 PointCloud2 攤平會產生上百萬筆,
    那不是「完整記錄」,那是把系統打死。
    """
    if depth > 6:
        return
    if isinstance(value, (bool, int, float, str)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            return
        out[prefix] = value
        return
    if isinstance(value, bytes):
        out[prefix + "/len"] = len(value)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > max_array:
            # 只記長度和頭尾,避免一個 LiDAR 掃描產生幾百筆
            out[prefix + "/len"] = len(value)
            return
        for i, item in enumerate(value):
            flatten(f"{prefix}/{i}", item, out, max_array, depth + 1)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            flatten(f"{prefix}/{k}", v, out, max_array, depth + 1)
        return
    # ROS 訊息物件:用它自己宣告的欄位
    fields = getattr(value, "get_fields_and_field_types", None)
    if callable(fields):
        for name in fields():
            flatten(f"{prefix}/{name}", getattr(value, name, None),
                    out, max_array, depth + 1)
        return
    out[prefix] = str(value)


class Ros2Source:
    """訂閱所有(或指定的)ROS 2 topic,把攤平後的純量交給 callback。

    callback 簽名跟 MQTT 那條路一樣:(topic_path, epoch_seconds, value)
    """

    def __init__(self, on_sample: Callable[[str, float, Any], None],
                 include: Optional[List[str]] = None,
                 exclude: Optional[List[str]] = None,
                 max_array: int = 8,
                 rediscover_s: float = 10.0,
                 node_name: str = "itri_fleet_agent"):
        self.on_sample = on_sample
        self.include = include or []
        self.exclude = list(exclude or []) + list(SKIP_TOPICS)
        self.max_array = max_array
        self.rediscover_s = rediscover_s
        self.node_name = node_name

        self._node = None
        self._exec = None
        self._thread: Optional[threading.Thread] = None
        self._subs: Dict[str, Any] = {}
        self._running = False
        self.skipped_types: Dict[str, str] = {}
        self.errors: List[str] = []

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node

        if not rclpy.ok():
            rclpy.init(args=None)
        self._node = Node(self.node_name)
        self._exec = SingleThreadedExecutor()
        self._exec.add_node(self._node)
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        self._discover()
        log.info("ROS 2 node %r 啟動,訂閱 %d 個 topic",
                 self.node_name, len(self._subs))

    def _spin(self) -> None:
        import rclpy
        last = 0.0
        while self._running:
            try:
                self._exec.spin_once(timeout_sec=0.2)
            except Exception as exc:
                if self._running:
                    log.warning("ROS 2 spin 錯誤: %s", exc)
                    time.sleep(0.5)
            # 車子開機時 node 是陸續上線的,要重複掃描才不會漏
            if time.time() - last > self.rediscover_s:
                last = time.time()
                try:
                    self._discover()
                except Exception as exc:
                    log.warning("重新探索 topic 失敗: %s", exc)
        try:
            rclpy.shutdown()
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    # ----------------------------------------------------------- discovery

    def _wanted(self, topic: str) -> bool:
        from .config import topic_matches

        # ROS 用 / 開頭,MQTT 樣式也用 /,所以同一套比對規則可以共用
        if any(topic == e or topic.startswith(e.rstrip("#").rstrip("/") + "/")
               or topic_matches(e, topic.lstrip("/")) for e in self.exclude):
            return False
        if not self.include:
            return True
        return any(topic == i or topic_matches(i, topic.lstrip("/"))
                   for i in self.include)

    def _discover(self) -> None:
        from rosidl_runtime_py.utilities import get_message

        for topic, types in self._node.get_topic_names_and_types():
            if topic in self._subs or not types:
                continue
            type_str = types[0]
            if type_str in SKIP_TYPES:
                self.skipped_types[topic] = type_str
                continue
            if not self._wanted(topic):
                continue
            try:
                msg_cls = get_message(type_str)
            except Exception as exc:
                self.errors.append(f"{topic}: 無法載入型別 {type_str} ({exc})")
                continue
            try:
                qos = self._match_qos(topic)
                self._subs[topic] = self._node.create_subscription(
                    msg_cls, topic, self._make_cb(topic), qos)
                log.info("訂閱 %s  (%s)", topic, type_str)
            except Exception as exc:
                self.errors.append(f"{topic}: 訂閱失敗 ({exc})")

    def _match_qos(self, topic: str):
        """照發佈端的 QoS 訂閱。

        這是 ROS 2 最常見的靜默失敗:感測器多半用 BEST_EFFORT 發佈,
        而預設訂閱是 RELIABLE,兩者不相容 —— 訂閱會成功建立,但一筆都收不到,
        也不會有任何錯誤訊息。
        """
        from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                               ReliabilityPolicy)

        profile = QoSProfile(depth=10, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.VOLATILE)
        try:
            infos = self._node.get_publishers_info_by_topic(topic)
        except Exception:
            return profile
        if not infos:
            return profile
        pub = infos[0].qos_profile
        # 只要有任何一個發佈端是 BEST_EFFORT,就跟著降級 —— RELIABLE 訂閱
        # 收不到 BEST_EFFORT 的訊息,反過來則可以。
        if any(i.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT
               for i in infos):
            profile.reliability = ReliabilityPolicy.BEST_EFFORT
        if pub.durability == DurabilityPolicy.TRANSIENT_LOCAL:
            profile.durability = DurabilityPolicy.TRANSIENT_LOCAL
        return profile

    # ------------------------------------------------------------ messages

    def _make_cb(self, topic: str):
        def cb(msg) -> None:
            now = time.time()
            flat: Dict[str, Any] = {}
            try:
                flatten(topic, msg, flat, self.max_array)
            except Exception as exc:
                self.errors.append(f"{topic}: 攤平失敗 ({exc})")
                return
            for path, value in flat.items():
                self.on_sample(path, now, value)
        return cb

    # --------------------------------------------------------------- stats

    def status(self) -> Dict[str, Any]:
        return {
            "subscribed": sorted(self._subs),
            "skipped_types": self.skipped_types,
            "errors": self.errors[-10:],
        }


def scan(seconds: float = 0.0, exclude: Optional[List[str]] = None,
         max_array: int = 8, refresh: float = 0.5):
    """discover 用:掃一段時間,回傳跟 MQTT 版一樣的 {path: TopicStat}。"""
    from .discover import TopicStat, _draw

    stats: Dict[str, TopicStat] = {}
    recent = set()

    def on_sample(path: str, ts: float, value: Any) -> None:
        st = stats.get(path)
        if st is None:
            st = stats[path] = TopicStat(path)
        if st.count and value != st.last_value:
            st.changed += 1
        st.count += 1
        st.bytes_total += len(str(value))
        st.last_value = value
        st.last_at = ts
        recent.add(path)

    src = Ros2Source(on_sample, exclude=exclude, max_array=max_array)
    src.start()

    import sys
    print(f"\x1b[2m訂閱 ROS 2 圖譜中的所有 topic。Ctrl-C 結束掃描\x1b[0m\n")
    t0 = time.time()
    drawn = 0
    try:
        while True:
            time.sleep(refresh)
            drawn = _draw(stats, recent, t0, drawn)
            recent.clear()
            if seconds and time.time() - t0 >= seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
    print()
    if src.skipped_types:
        print(f"\x1b[2m略過 {len(src.skipped_types)} 個大型 topic:"
              f"{', '.join(list(src.skipped_types)[:4])}…\x1b[0m")
    for e in src.errors[:5]:
        print(f"\x1b[33m  ! {e}\x1b[0m")
    return stats
