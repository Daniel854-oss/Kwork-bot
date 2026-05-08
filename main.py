"""Entry point: launches both Telegram bots in one process."""

import asyncio
import logging
import signal
import traceback

from accounts import AccountManager
from bot_orders import build_orders_bot, poll_kwork, polling_paused, stats, BUILD_VERSION
from bot_messages import build_messages_bot, poll_messages
from bot_messages import stats as msg_stats
from config import TG_BOT_TOKEN_ORDERS, TG_BOT_TOKEN_MESSAGES, TG_CHAT_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def order_poll_loop(app):
    """Самостоятельный цикл проверки заказов. Запускается из main — не зависит от post_init."""
    from accounts import AccountManager
    from storage import load_keywords, load_blacklist

    log.info("=== ORDER POLL LOOP STARTED ===")

    # Gather connects info
    connects_info = ""
    try:
        # Get account manager from bot_orders
        from bot_orders import account_mgr
        if account_mgr:
            for acc in account_mgr.accounts:
                try:
                    async with acc.create_api() as api:
                        c = await api.get_connects()
                        connects_info += f"  {acc.name}: {c.active_connects}/{c.all_connects} коннектов\n"
                except Exception as e:
                    connects_info += f"  {acc.name}: ⚠️ {e}\n"
    except Exception:
        pass

    kws = load_keywords()
    bl = load_blacklist()

    try:
        await app.bot.send_message(
            TG_CHAT_ID,
            f"🟢 Бот запущен!\n"
            f"📦 Версия: {BUILD_VERSION}\n"
            f"🔑 Keywords: {len(kws)} | 🚫 Blacklist: {len(bl)}\n"
            f"🔄 Поллинг: asyncio loop ✅\n"
            f"💎 Коннекты:\n{connects_info}"
            f"⏱ Первая проверка через 10 сек..."
        )
    except Exception as e:
        log.error("Failed to send startup msg: %s", e)

    await asyncio.sleep(10)
    log.info("=== FIRST POLL STARTING ===")

    while True:
        if not polling_paused:
            try:
                await poll_kwork(app)
                log.info("Poll #%d done", stats["polls"])
            except Exception:
                stats["errors"] += 1
                tb = traceback.format_exc()
                log.error("Polling error: %s", tb)
                try:
                    await app.bot.send_message(TG_CHAT_ID, f"❗ Ошибка поллинга:\n{tb[:3000]}")
                except Exception:
                    pass
        await asyncio.sleep(60)


async def messages_poll_loop(app):
    """Цикл проверки сообщений. Запускается из main — не зависит от post_init."""
    log.info("=== MESSAGES POLL LOOP STARTED ===")

    await asyncio.sleep(10)

    # Silent first run: seed seen_data with current unreads (no notifications)
    try:
        await poll_messages(app, silent=True)
        log.info("Messages: silent seed complete")
    except Exception as e:
        log.error("Messages silent seed failed: %s", e)

    try:
        await app.bot.send_message(
            TG_CHAT_ID,
            "📩 Бот сообщений запущен!\n"
            "🔄 Проверяю диалоги каждые 30 сек..."
        )
    except Exception as e:
        log.error("Failed to send messages startup msg: %s", e)

    while True:
        try:
            await poll_messages(app)
        except Exception:
            msg_stats["errors"] += 1
            tb = traceback.format_exc()
            log.error("Messages polling error: %s", tb)
            try:
                await app.bot.send_message(TG_CHAT_ID, f"❗ Ошибка бота сообщений:\n{tb[:3000]}")
            except Exception:
                pass
        await asyncio.sleep(30)


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

    # START POLL LOOPS HERE — guaranteed to run, references kept alive in run() scope
    poll_task = asyncio.create_task(order_poll_loop(orders_app))
    log.info("Orders poll task created: %s", poll_task)

    msg_poll_task = None
    if messages_app:
        msg_poll_task = asyncio.create_task(messages_poll_loop(messages_app))
        log.info("Messages poll task created: %s", msg_poll_task)

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
        poll_task.cancel()
        if msg_poll_task:
            msg_poll_task.cancel()
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
