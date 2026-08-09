"""確認 agent 套件裡沒有夾帶這台伺服器的任何資料。

套件會發給每一台車,而 `pyproject.toml` 把 README 打包進 wheel 的 METADATA ——
在說明文字裡順手寫下真實網址或真實 token 很容易,而且事後看不出來。這支腳本
把那件事變成會失敗的檢查,而不是要靠記性。

  python tools/check_package.py            檢查原始碼與已建置的 wheel
  python tools/check_package.py --wheel X  只檢查某個 wheel
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"

# 佔位符可以出現,真值不行
ALLOW = {"XXXX-XXXX-XXXX", "tskey-auth-XXXXX", "tskey-auth-XXXX"}

PATTERNS: Dict[str, str] = {
    "tailnet 網址":     r"[\w-]+\.ts\.net",
    "Tailscale IP":     r"\b100\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}\b",
    "資料庫密碼":        r"itri_fleet_dev",
    "資料庫 DSN":        r"postgres(?:ql)?://",
    "enroll token":     r"\b[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\b",
    "Tailscale authkey": r"tskey-auth-\S+",
    "VAPID 私鑰":        r"\bprivate_key\b\s*[:=]",
    "本機使用者/路徑":    r"\bag133\b|[A-Za-z]:\\Users\\",
    "email":            r"[\w.+-]+@[\w-]+\.[\w.]+",
}


def scan_text(name: str, text: str) -> List[Tuple[str, str, int]]:
    out = []
    for label, pat in PATTERNS.items():
        for m in re.finditer(pat, text):
            if m.group(0) in ALLOW:
                continue
            line = text[: m.start()].count("\n") + 1
            out.append((label, m.group(0), line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    problems = 0
    checked = 0

    print("=== 原始碼 ===")
    for p in sorted(AGENT.rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".md", ".toml", ".cfg", ".txt"):
            continue
        if "build" in p.parts or "egg-info" in str(p) or "dist" in p.parts:
            continue
        checked += 1
        for label, hit, line in scan_text(p.name, p.read_text(encoding="utf-8", errors="replace")):
            problems += 1
            print(f"  X {p.relative_to(ROOT)}:{line}  {label} -> {hit}")

    wheels = [Path(args.wheel)] if args.wheel else sorted((AGENT / "dist").glob("*.whl"))
    for w in wheels:
        print(f"\n=== wheel: {w.name} ===")
        z = zipfile.ZipFile(w)
        for name in z.namelist():
            if name.endswith("/"):
                continue
            checked += 1
            text = z.read(name).decode("utf-8", "replace")
            for label, hit, line in scan_text(name, text):
                problems += 1
                print(f"  X {name}:{line}  {label} -> {hit}")

    print(f"\n檢查 {checked} 個檔案")
    if problems:
        print(f"發現 {problems} 處夾帶資料 —— 換成佔位符後重新 pip wheel")
        return 1
    print("乾淨:套件裡沒有這台伺服器的任何資料")
    print("  伺服器位址   由 enroll 的 --server 參數帶入")
    print("  MQTT 憑證    由伺服器產生,存在車上的 ~/.itri-fleet/credentials.json")
    print("  同一個 wheel 給誰用都一樣,不含任何識別資訊")
    return 0


if __name__ == "__main__":
    sys.exit(main())
