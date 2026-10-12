from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import Settings


def code_fingerprint() -> str:
    manifest = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  app/{path.name}\n"
        for path in sorted(Path(__file__).parent.glob("*.py"))
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


def mismatched_keys(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    return sorted(key for key, value in expected.items()
                  if actual.get(key) != str(value))


def main() -> int:
    parser = argparse.ArgumentParser(description="核对运行配置和部署代码，不显示凭据")
    parser.add_argument("--expected-code")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        Settings.from_env(create_directories=False)
        if not args.validate_only:
            services = json.load(sys.stdin)["services"]
            # Legacy TG115 compatibility for already-written Compose environment.
            service_name = os.getenv(
                "TG2CLOUD_COMPOSE_SERVICE",
                os.getenv("TG115_COMPOSE_SERVICE", "tg2cloud-clouddrive2-bot"),
            )
            desired = services[service_name]["environment"]
            changed = mismatched_keys(desired, dict(os.environ))
            if changed:
                print("TG2CLOUD_CONFIG=MISMATCH_KEYS:" + ",".join(changed))
                return 1
            if not args.expected_code or code_fingerprint() != args.expected_code:
                print("TG2CLOUD_CODE=MISMATCH")
                return 1
            print("TG2CLOUD_CODE=OK")
        print(f"TG2CLOUD_VERSION={__version__}")
        print("TG2CLOUD_CONFIG=OK")
        return 0
    except Exception:  # noqa: BLE001 - never expose config values at this CLI boundary
        print("TG2CLOUD_CONFIG=FAILED（配置格式或必填项错误；未输出配置值）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
