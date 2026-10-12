"""High-speed download adapter using local C++ telegram-bot-api."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional, Union

from telethon import utils

log = logging.getLogger("tg2cloud.botapi")

API_BASE = os.getenv("LOCAL_BOT_API_URL", "http://127.0.0.1:8082")
BOT_API_MOUNT = os.getenv("BOT_API_DATA_DIR", "/bot-api-data")
MAX_BOT_API_SIZE = 2000 * 1024 * 1024  # 2000MB limit in Telegram Bot API


def cleanup_stale_bot_api_files(api_mount: str = BOT_API_MOUNT, max_age_seconds: int = 3600) -> None:
    """Clean up orphaned temp or media files in bot-api-data older than max_age_seconds."""
    mount_path = Path(api_mount)
    if not mount_path.is_dir():
        return
    now = time.time()
    try:
        for token_dir in mount_path.iterdir():
            if not token_dir.is_dir() or ":" not in token_dir.name:
                continue
            for sub in ("temp", "videos", "documents", "music", "photos", "voice", "video_notes"):
                target_dir = token_dir / sub
                if not target_dir.is_dir():
                    continue
                for item in target_dir.iterdir():
                    try:
                        if item.is_file() and (now - item.stat().st_mtime > max_age_seconds):
                            item.unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        pass


def _fetch_file_info_sync(api_base: str, bot_token: str, file_id: str, timeout: int = 1800) -> dict:
    url = f"{api_base}/bot{bot_token}/getFile?file_id={urllib.parse.quote(file_id)}"
    req = urllib.request.Request(url, headers={"User-Agent": "tg2cloud/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def local_bot_api_download(
    client: Any,
    bot_token: str,
    message: Any,
    out_file: Union[str, Path, Any],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    api_base: str = API_BASE,
    api_mount: str = BOT_API_MOUNT,
) -> Any:
    """Download file using local telegram-bot-api server with real-time progress."""
    media = getattr(message, "media", message)
    if not media:
        return await client.download_media(message, file=out_file, progress_callback=progress_callback)

    total_size = getattr(getattr(media, "document", None), "size", 0) or 0
    if total_size <= 0:
        info = utils._get_file_info(media)
        total_size = info.size if info else 0

    # If file exceeds Bot API's 2000MB limit, fallback to Telethon
    if total_size > MAX_BOT_API_SIZE:
        log.info("文件大小 (%s) 超过 Bot API 2GB 上限，使用 Telethon 下载", total_size)
        return await client.download_media(message, file=out_file, progress_callback=progress_callback)

    try:
        bot_file_id = utils.pack_bot_file_id(media)
    except Exception as exc:
        log.warning("无法打包 bot_file_id (%s)，回退至 Telethon", exc)
        return await client.download_media(message, file=out_file, progress_callback=progress_callback)

    stop_monitor = asyncio.Event()
    start_time = time.time()

    async def _progress_monitor():
        temp_dir = Path(api_mount) / bot_token / "temp"
        last_reported = 0
        while not stop_monitor.is_set():
            await asyncio.sleep(0.5)
            try:
                if temp_dir.exists():
                    # Only track temp files modified after download started
                    files = [
                        f for f in temp_dir.iterdir()
                        if f.is_file() and f.stat().st_mtime >= start_time - 2
                    ]
                    if files:
                        largest = max(f.stat().st_size for f in files)
                        if largest > last_reported:
                            last_reported = largest
                            if progress_callback:
                                progress_callback(largest, total_size or largest)
            except Exception:
                pass

    monitor_task = asyncio.create_task(_progress_monitor())
    http_timeout = max(1800, int((total_size or 0) / (1024 * 1024) * 2))
    disk_path: Optional[Path] = None

    try:
        res = await asyncio.to_thread(_fetch_file_info_sync, api_base, bot_token, bot_file_id, http_timeout)
    except Exception as exc:
        log.warning("本地 Bot API getFile 失败 (%s)，回退至 Telethon", exc)
        return await client.download_media(message, file=out_file, progress_callback=progress_callback)
    finally:
        stop_monitor.set()
        await monitor_task

    if not res.get("ok"):
        log.warning("本地 Bot API 返回错误：%s，回退至 Telethon", res)
        return await client.download_media(message, file=out_file, progress_callback=progress_callback)

    remote_file_path = res["result"].get("file_path", "")
    prefix = "/var/lib/telegram-bot-api/"
    rel_path = remote_file_path[len(prefix):] if remote_file_path.startswith(prefix) else remote_file_path.lstrip("/")
    disk_path = Path(api_mount) / rel_path

    if not disk_path.is_file():
        log.warning("本地 Bot API 下载文件未在磁盘中找到：%s，回退至 Telethon", disk_path)
        return await client.download_media(message, file=out_file, progress_callback=progress_callback)

    try:
        # Deliver to local path
        if isinstance(out_file, (str, Path)):
            target_path = Path(out_file)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            # Use shutil.move for zero-copy OS transfer (sendfile) and atomic disk space release
            shutil.move(str(disk_path), str(target_path))
            if progress_callback:
                progress_callback(total_size or target_path.stat().st_size, total_size or target_path.stat().st_size)
            return str(target_path)
        elif hasattr(out_file, "write"):
            written = 0
            with open(disk_path, "rb") as src:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    r = out_file.write(buf)
                    if inspect.isawaitable(r):
                        await r
                    written += len(buf)
                    if progress_callback:
                        progress_callback(written, total_size or written)
            return out_file
        else:
            return await client.download_media(message, file=out_file, progress_callback=progress_callback)
    finally:
        if disk_path is not None:
            try:
                if disk_path.exists():
                    disk_path.unlink()
            except Exception:
                pass