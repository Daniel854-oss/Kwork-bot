"""Entry point: launches both Telegram bots in one process."""

import asyncio
import logging
import signal

from accounts import AccountManager
from bot_orders import build_orders_bot
from bot_messages import build_messages_bot
from config import TG_BOT_TOKEN_ORDERS, TG_BOT_TOKEN_MESSAGES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def run():
    mgr = AccountManager()

    orders_app = build_orders_bot(mgr)
    log.info("Orders bot built")

    # Only start messages bot if token is configured
    messages_app = None
    if TG_BOT_TOKEN_MESSAGES:
        messages_app = build_messages_bot(mgr)
        log.info("Messages bot built")
    else:
        log.warning("TG_BOT_TOKEN_MESSAGES not set — messages bot disabled")

    # Initialize and start
    await orders_app.initialize()
    await orders_app.start()
    await orders_app.updater.start_polling()
    log.info("Orders bot started polling")

    if messages_app:
        await messages_app.initialize()
        await messages_app.start()
        await messages_app.updater.start_polling()
        log.info("Messages bot started polling")

    # Keep running until interrupted
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log.info("Shutting down...")
        await orders_app.updater.stop()
        await orders_app.stop()
        await orders_app.shutdown()
        if messages_app:
            await messages_app.updater.stop()
            await messages_app.stop()
            await messages_app.shutdown()


def main():
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")


if __name__ == "__main__":
    main()
