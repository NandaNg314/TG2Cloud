"""High-speed parallel file downloader routing via local C++ Bot API or Telethon."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

from telethon import utils
from .bot_api_download import local_bot_api_download

log = logging.getLogger("tg2cloud.download")


async def parallel_download_media(
    client: Any,
    message: Any,
    *,
    file: Any,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_connections: int = 5,
    bot_token: Optional[str] = None,
) -> Any:
    """Download media using local telegram-bot-api with automatic Telethon fallback."""
    if bot_token:
        try:
            return await local_bot_api_download(
                client,
                bot_token,
                message,
                file,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            log.warning("本地 Bot API 下载异常 (%s)，平滑回退至 Telethon...", exc)

    # Fallback to Telethon standard download
    return await client.download_media(message, file=file, progress_callback=progress_callback)