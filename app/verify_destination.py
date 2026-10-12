from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable

from .config import Settings
from .rclone_client import MoveUncertainError, RcloneClient


class DestinationVerificationError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


async def verify_destination(
    settings: Settings,
    client: RcloneClient | None = None,
    report: Callable[[str], None] | None = None,
) -> str:
    rclone = client or RcloneClient(settings)
    destination_label = getattr(settings, "destination_label", "CloudDrive2")
    token = uuid.uuid4().hex
    local_path = settings.data_dir / f".tg115-verify-{token}.bin"
    remote_temp = f".tg115-verify-{token}.uploading"
    remote_final = f".tg115-verify-{token}.ok"
    direct_final = getattr(settings, "storage_backend", "clouddrive2") == "openlist"
    remote_write = remote_final if direct_final else remote_temp
    payload = os.urandom(256)
    verification_succeeded = False
    move_uncertain = False
    stage = "AUTH"

    def emit(value: str) -> None:
        if report is not None:
            report(value)

    local_path.write_bytes(payload)
    try:
        try:
            await rclone.verify_authentication()
        except Exception as exc:
            raise DestinationVerificationError(stage, str(exc)) from exc
        emit("TG2CLOUD_WEBDAV_AUTH=OK")
        stage = "LIST"
        try:
            await rclone.prepare_destination()
        except Exception as exc:
            raise DestinationVerificationError(stage, str(exc)) from exc
        emit("TG2CLOUD_WEBDAV_LIST=OK")
        emit("TG2CLOUD_WEBDAV=OK")
        stage = "WRITE"
        await rclone.upload(local_path, remote_write)
        emit("TG2CLOUD_WEBDAV_WRITE=OK")
        stage = "SIZE"
        uploaded_size = await rclone.remote_size(remote_write)
        if uploaded_size != len(payload):
            raise RuntimeError(
                f"{destination_label} WebDAV 临时测试文件大小错误："
                f"{uploaded_size} != {len(payload)}"
            )
        emit("TG2CLOUD_WEBDAV_SIZE=OK")
        emit("TG2CLOUD_UPLOAD=OK")
        if direct_final:
            emit("TG2CLOUD_WEBDAV_FINALIZE_MODE=DIRECT")
            emit("TG2CLOUD_WEBDAV_MOVE=NOT_REQUIRED")
            emit("TG2CLOUD_RENAME=NOT_REQUIRED")
        else:
            stage = "MOVE"
            await rclone.move(remote_temp, remote_final)
            final_size = await rclone.remote_size(remote_final)
            if final_size != len(payload):
                raise RuntimeError(
                    f"{destination_label} WebDAV 最终测试文件大小错误："
                    f"{final_size} != {len(payload)}"
                )
            if await rclone.exists(remote_temp):
                raise RuntimeError(
                    f"{destination_label} WebDAV 临时测试文件改名后仍然存在"
                )
            emit("TG2CLOUD_WEBDAV_MOVE=OK")
            emit("TG2CLOUD_RENAME=OK")
        stage = "DELETE"
        await rclone.remove(remote_final)
        if await rclone.exists(remote_final):
            raise RuntimeError(
                f"{destination_label} WebDAV 测试文件清理失败"
            )
        emit("TG2CLOUD_WEBDAV_DELETE=OK")
        emit("TG2CLOUD_DELETE=OK")
        verification_succeeded = True
        return remote_final
    except DestinationVerificationError:
        raise
    except Exception as exc:
        move_uncertain = isinstance(exc, MoveUncertainError)
        raise DestinationVerificationError(stage, str(exc)) from exc
    finally:
        try:
            local_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not verification_succeeded and not move_uncertain and stage not in {"AUTH", "LIST"}:
            cleanup_paths = (remote_final,) if direct_final else (
                remote_temp, remote_final,
            )
            for remote_path in cleanup_paths:
                try:
                    await rclone.remove(remote_path)
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    print(
                        f"TG2CLOUD_CLEANUP_WARNING={remote_path}: {exc}",
                        file=sys.stderr,
                    )


async def async_main() -> int:
    settings = Settings.from_env()
    print(f"TG2CLOUD_STORAGE_GATEWAY={settings.storage_backend}")
    remote_path = await verify_destination(settings, report=print)
    print("TG2CLOUD_DESTINATION=OK")
    print(f"TG2CLOUD_TEST_PATH={remote_path}")
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except DestinationVerificationError as exc:
        print(f"TG2CLOUD_WEBDAV_{exc.stage}=FAILED", file=sys.stderr)
        aggregate = {
            "AUTH": "WEBDAV",
            "LIST": "WEBDAV",
            "WRITE": "UPLOAD",
            "SIZE": "UPLOAD",
            "MOVE": "RENAME",
            "DELETE": "DELETE",
        }.get(exc.stage)
        if aggregate:
            print(f"TG2CLOUD_{aggregate}=FAILED", file=sys.stderr)
        print(f"TG2CLOUD_DESTINATION=FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI error boundary
        print(f"TG2CLOUD_DESTINATION=FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
