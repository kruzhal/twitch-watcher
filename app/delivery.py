"""Durable Telegram delivery independent from Twitch socket reads."""
import asyncio
import logging
import re
import time
from html import unescape
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError, TelegramForbiddenError
from .health import NullMonitor, safe_error

log = logging.getLogger(__name__)

async def deliver_notifications(bot, storage, chat_id, monitor=None):
    monitor = monitor or NullMonitor()
    delay = 2
    while True:
        try:
            row = await storage.next_notification()
            if row is None:
                monitor.update("delivery", phase="idle", error=None)
                await asyncio.sleep(1)
                continue
            notification_id, body = row
            monitor.update("delivery", phase="sending")
            try:
                await bot.send_message(chat_id, body, parse_mode="HTML", disable_web_page_preview=True)
            except TelegramBadRequest:
                # Keep malformed formatting from blocking all later notifications.
                plain = unescape(re.sub(r"<[^>]*>", "", body))
                await bot.send_message(chat_id, plain[:4000], parse_mode=None)
            await storage.acknowledge_notification(notification_id)
            monitor.update("delivery", phase="ready", last_success_at=time.time(), error=None)
            delay = 2
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            monitor.update("delivery", phase="retrying", error=safe_error(exc), error_at=time.time())
            await asyncio.sleep(max(1, exc.retry_after))
        except (TelegramNetworkError, TelegramForbiddenError, OSError, asyncio.TimeoutError) as exc:
            monitor.update("delivery", phase="retrying", error=safe_error(exc), error_at=time.time())
            log.warning("Notification retained (%s); retry in %ss", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except Exception as exc:
            monitor.update("delivery", phase="retrying", error=safe_error(exc), error_at=time.time())
            log.exception("Notification delivery failed; retained for retry")
            await asyncio.sleep(10)
