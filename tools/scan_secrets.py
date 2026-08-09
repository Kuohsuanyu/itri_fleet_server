"""推上 GitHub 前掃一遍,確認沒有任何金鑰跟著走。

    python tools/scan_secrets.py            掃工作目錄
    python tools/scan_secrets.py --staged   只掃 git 已暫存的內容(pre-commit 用)

`.gitignore` 擋得住「沒被加進去的檔案」,擋不住「不小心寫進程式碼或說明裡的
金鑰」。這支掃的是後者 —— 那正是先前 agent wheel 夾帶真實 token 的原因。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent

TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".cfg",
                 ".txt", ".sql", ".bat", ".sh", ".js", ".html", ".css", ""}

SKIP_DIRS = {".git", "__pycache__", "build", "dist", "node_modules",
             "backups", "logs", ".venv", "venv"}

# 佔位符是允許的;真值不行
ALLOW = re.compile(
    r"CHANGE_ME|CHANGE-ME|GENERATED_BY_SETUP|你的|XXXX|xxxxx|ZZZZ|YOUR-|"
    r"example\.com|<[^>]+>|\{[a-z_]+\}|tskey-auth-XXXX|localhost|127\.0\.0\.1")

PATTERNS: Dict[str, str] = {
    "VAPID/私鑰(長 base64url)": r"\b[A-Za-z0-9_-]{42,}\b",
    "Tailscale auth key":        r"tskey-[a-z]+-\S+",
    "資料庫 DSN 含密碼":          r"postgres(?:ql)?://[^:\s]+:[^@\s]+@",
    "tailnet 主機名":            r"\b[\w-]+\.ts\.net\b",
    "Tailscale IP":              r"\b100\.(?:\d{1,3}\.){2}\d{1,3}\b",
    "enroll token":              r"\b[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}\b",
    "AWS key":                   r"AKIA[0-9A-Z]{16}",
    "私鑰檔頭":                   r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "本機使用者路徑":              r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+",
    "個人信箱":                   r"[\w.+-]+@(?!example\.com)[\w-]+\.[\w.]{2,}",
}

# 這些檔案本身就是在描述樣式,會自然命中
SELF_REFERENTIAL = {"tools/scan_secrets.py", "tools/check_package.py"}


def git_tracked() -> Optional[List[Path]]:
    """Exactly the files git would put in a commit.

    Asking git directly beats filtering our own walk through `check-ignore`:
    the semantics are the ones that actually matter, and it sidesteps a quirk
    where `check-ignore --stdin` returns nothing when driven from subprocess
    on Windows even though the same pipe works from a shell.
    """
    if not (ROOT / ".git").exists():
        return None
    r = subprocess.run(["git", "ls-files", "--cached", "--others",
                        "--exclude-standard", "-z"],
                       cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        return None
    names = r.stdout.decode("utf-8", "replace").split("\0")
    return [ROOT / n for n in names if n and (ROOT / n).is_file()]


def iter_files(staged: bool) -> List[Path]:
    if staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"],
                             cwd=ROOT, capture_output=True)
        names = out.stdout.decode("utf-8", "replace").split("\0")
        return [ROOT / n for n in names if n and (ROOT / n).is_file()]

    tracked = git_tracked()
    if tracked is None:                      # not a repo yet -- scan everything
        tracked = [p for p in ROOT.rglob("*") if p.is_file()
                   and not any(d in p.parts for d in SKIP_DIRS)]
    return [p for p in tracked if p.suffix.lower() in TEXT_SUFFIXES]


def scan(path: Path) -> List[Tuple[str, str, int]]:
    rel = path.relative_to(ROOT).as_posix()
    if rel in SELF_REFERENTIAL:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits = []
    for label, pat in PATTERNS.items():
        for m in re.finditer(pat, text):
            v = m.group(0)
            if ALLOW.search(v):
                continue
            # 長 base64 很容易誤判(hash、雜湊、base64 圖檔)
            if label.startswith("VAPID") and not re.search(
                    r"(?i)(private_key|secret|vapid|password)\s*[:=]\s*\S{0,4}$",
                    text[max(0, m.start() - 60):m.start()]):
                continue
            line = text[: m.start()].count("\n") + 1
            hits.append((label, v, line))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    files = iter_files(args.staged)
    total = 0
    for f in sorted(files):
        for label, value, line in scan(f):
            total += 1
            shown = value if len(value) < 56 else value[:53] + "..."
            print(f"  X {f.relative_to(ROOT).as_posix()}:{line}  {label}  ->  {shown}")

    print(f"\n  掃描 {len(files)} 個檔案")
    if total:
        print(f"  {total} 處疑似機密 —— 換成佔位符或移到 config.yaml(已被 gitignore)")
        return 1
    if not args.quiet:
        print("  乾淨,可以推上公開 repo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
