import asyncio
import logging
import time

from aiogram.exceptions import TelegramNetworkError

from .bot import setup_bot
from .delivery import deliver_notifications
from .config import Config
from .storage import Storage
from .twitch import TwitchClient
from .health import RuntimeMonitor, NullMonitor, safe_error
from .version import VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def run_telegram(dispatcher, bot, monitor=None):
    monitor = monitor or NullMonitor()
    delay = 2
    while True:
        try:
            await bot.get_me(request_timeout=10)
            monitor.update("telegram", available=True, polling=True, last_success_at=time.time(), error=None)
            delay = 2
            await dispatcher.start_polling(bot, polling_timeout=15, close_bot_session=False)
            return
        except asyncio.CancelledError:
            raise
        except (TelegramNetworkError, OSError, asyncio.TimeoutError) as exc:
            monitor.update("telegram", available=False, polling=False, error=safe_error(exc), error_at=time.time())
            logging.getLogger(__name__).warning(
                "Telegram unavailable (%s); retrying in %s seconds", type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def probe_telegram(bot, monitor):
    while True:
        try:
            await bot.get_me(request_timeout=10)
            monitor.update("telegram", available=True, last_success_at=time.time(), error=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            monitor.update("telegram", available=False, error=safe_error(exc), error_at=time.time())
        await asyncio.sleep(30)


async def main():
    config = Config()
    config.validate()
    storage = Storage(config.database_path)
    await storage.init()
    monitor = RuntimeMonitor(storage)
    monitor.update("twitch", connected=False, active=0, phase="starting")
    monitor.update("telegram", available=False, phase="starting")
    await monitor.flush()
    logging.getLogger(__name__).info("Starting Twitch Watcher %s", VERSION)

    notify_handler = {}

    async def on_message(event):
        await notify_handler["notify"](event)

    twitch = TwitchClient(config, storage, on_message, monitor=monitor)
    bot, dispatcher, notify, _ = setup_bot(config, storage, twitch)
    notify_handler["notify"] = notify

    tasks = set()
    try:
        tasks = {
            asyncio.create_task(run_telegram(dispatcher, bot, monitor), name="telegram-polling"),
            asyncio.create_task(twitch.run(), name="twitch-eventsub"),
            asyncio.create_task(deliver_notifications(bot, storage, config.telegram_admin_id, monitor), name="notification-delivery"),
            asyncio.create_task(monitor.run(), name="runtime-heartbeat"),
            asyncio.create_task(probe_telegram(bot, monitor), name="telegram-health"),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
