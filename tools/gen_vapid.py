"""Generate a VAPID key pair for Web Push.

Run once per deployment and paste the result into config.yaml under
`alerts.channels.push`. Changing the keys invalidates every existing
subscription, so devices have to re-enable notifications.

  python tools/gen_vapid.py
"""

from __future__ import annotations

import base64
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    print("把下面兩行貼進 config.yaml 的 alerts.channels.push:\n")
    print(f"      public_key: {b64(public)}")
    print(f"      private_key: {b64(private)}")
    print("\n⚠ private_key 是機密,不要提交到版本控制。")
    print("⚠ 換掉金鑰會讓所有已訂閱的裝置失效,需要重新開啟通知。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
