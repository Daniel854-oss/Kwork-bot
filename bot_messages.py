"""Bot 2: Message monitoring — instant notifications when clients write."""

import asyncio
import logging
import traceback
from datetime import datetime

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from accounts import AccountManager
from ai import generate_reply
from config import TG_BOT_TOKEN_MESSAGES, TG_CHAT_ID
from storage import load_seen_msgs, save_seen_msgs

log = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")

# In-memory storage for pending message replies
pending_replies: dict[str, dict] = {}

# Global account manager
account_mgr: AccountManager | None = None


# ── Polling messages ──────────────────────────────────────

async def poll_messages(app: Application):
    """Check all accounts for new unread messages."""
    seen_data = load_seen_msgs()

    for acc in account_mgr.accounts:
        acc_seen = set(seen_data.get(acc.id, []))
        try:
            async with acc.create_api() as api:
                dialogs = await api.get_all_dialogs()
        except Exception as e:
            log.error("Error polling messages %s: %s", acc.name, e)
            continue

        for dialog in dialogs:
            # Skip dialogs with no unread messages
            unread = getattr(dialog, "unread", 0) or 0
            unread_count = getattr(dialog, "unread_count", 0) or 0
            if unread == 0 and unread_count == 0:
                continue

            user_id = getattr(dialog, "user_id", None)
            username = getattr(dialog, "username", None) or "unknown"
            last_msg = getattr(dialog, "last_message", None) or ""
            msg_time = getattr(dialog, "time", 0) or 0

            # Use composite key: account_id:user_id:time to deduplicate
            msg_key = f"{acc.id}:{user_id}:{msg_time}"
            if msg_time in acc_seen:
                continue

            # Get full conversation for context
            context_text = ""
            try:
                async with acc.create_api() as api:
                    messages = await api.get_dialog_with_user(username)
                    # Get last 5 messages for context
                    recent = messages[-5:] if len(messages) > 5 else messages
                    context_lines = []
                    for m in recent:
                        sender = getattr(m, "from_username", "?")
                        text = getattr(m, "message", "") or ""
                        context_lines.append(f"{sender}: {text[:200]}")
                    context_text = "\n".join(context_lines)
            except Exception as e:
                log.warning("Could not fetch dialog context for %s: %s", username, e)
                context_text = last_msg

            # Store for reply generation
            reply_key = f"{acc.id}:{user_id}"
            pending_replies[reply_key] = {
                "account_id": acc.id,
                "account_name": acc.name,
                "user_id": user_id,
                "username": username,
                "last_message": last_msg,
                "context": context_text,
                "msg_time": msg_time,
            }

            # Send notification
            text = (
                f"📩 Новое сообщение\n"
                f"🏷️ Аккаунт: {acc.name}\n"
                f"👤 {username}\n\n"
                f"💬 {last_msg[:500]}\n"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💡 Сгенерировать ответ", callback_data=f"mreply:{reply_key}"),
                    InlineKeyboardButton("🔗 Kwork", url=f"https://kwork.ru/dialog?user={username}"),
                ]
            ])
            await app.bot.send_message(chat_id=TG_CHAT_ID, text=text, reply_markup=keyboard)

            acc_seen.add(msg_time)

        seen_data[acc.id] = list(acc_seen)

    save_seen_msgs(seen_data)


async def message_polling_loop(app: Application):
    while True:
        try:
            await poll_messages(app)
        except Exception:
            tb = traceback.format_exc()
            try:
                await app.bot.send_message(TG_CHAT_ID, f"❗ Ошибка бота сообщений:\n{tb[:3000]}")
            except Exception:
                pass
        await asyncio.sleep(30)


# ── Callback handler ─────────────────────────────────────

def reply_keyboard(reply_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Отправить", callback_data=f"msend:{reply_key}"),
            InlineKeyboardButton("🔄 Переписать", callback_data=f"mregen:{reply_key}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"mcancel:{reply_key}"),
        ]
    ])


async def do_generate_reply(query, reply_data: dict, reply_key: str):
    try:
        reply_text = await generate_reply(
            message=reply_data["last_message"],
            context=reply_data.get("context", ""),
            account_id=reply_data["account_id"],
        )
        reply_data["generated_reply"] = reply_text
        pending_replies[reply_key] = reply_data
    except Exception as e:
        await query.message.reply_text(f"❗ Ошибка генерации: {e}")
        return

    text = (
        f"💬 Ответ для {reply_data['username']} ({reply_data['account_name']}):\n\n"
        f"{reply_text}"
    )
    await query.message.reply_text(text, reply_markup=reply_keyboard(reply_key))


async def on_msg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("mreply:"):
        reply_key = data[len("mreply:"):]
        reply_data = pending_replies.get(reply_key)
        if not reply_data:
            await query.message.reply_text("❗ Сообщение не найдено в памяти.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⏳ Генерирую ответ для {reply_data['username']}...")
        await do_generate_reply(query, reply_data, reply_key)

    elif data.startswith("mregen:"):
        reply_key = data[len("mregen:"):]
        reply_data = pending_replies.get(reply_key)
        if not reply_data:
            await query.message.reply_text("❗ Данные не найдены.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Генерирую новый вариант...")
        await do_generate_reply(query, reply_data, reply_key)

    elif data.startswith("msend:"):
        reply_key = data[len("msend:"):]
        reply_data = pending_replies.get(reply_key)
        if not reply_data:
            await query.message.reply_text("❗ Данные не найдены.")
            return
        await query.edit_message_reply_markup(reply_markup=None)

        generated = reply_data.get("generated_reply", "")
        if not generated:
            await query.message.reply_text("❗ Нет сгенерированного ответа.")
            return

        acc = account_mgr.get(reply_data["account_id"])
        if not acc:
            await query.message.reply_text("❗ Аккаунт не найден.")
            return

        await query.message.reply_text(f"⏳ Отправляю ответ от {acc.name}...")
        try:
            async with acc.create_api() as api:
                await api.send_message(user_id=reply_data["user_id"], text=generated)
            pending_replies.pop(reply_key, None)
            await query.message.reply_text(f"✅ Ответ отправлен от {acc.name}!")
        except Exception as e:
            await query.message.reply_text(f"❗ Ошибка отправки: {e}")

    elif data.startswith("mcancel:"):
        reply_key = data[len("mcancel:"):]
        pending_replies.pop(reply_key, None)
        await query.edit_message_reply_markup(reply_markup=None)


# ── Commands ──────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(MSK)
    seen_data = load_seen_msgs()
    total_seen = sum(len(v) for v in seen_data.values())
    await update.message.reply_text(
        f"📩 Бот сообщений\n"
        f"🕐 Время МСК: {now.strftime('%H:%M')}\n"
        f"👥 Аккаунтов: {len(account_mgr.accounts)}\n"
        f"📨 Обработано сообщений: {total_seen}\n"
        f"⏱ Интервал проверки: 30 сек\n"
    )


# ── Build & run ──────────────────────────────────────────

async def post_init(app: Application):
    asyncio.create_task(message_polling_loop(app))


def build_messages_bot(mgr: AccountManager) -> Application:
    global account_mgr
    account_mgr = mgr

    app = Application.builder().token(TG_BOT_TOKEN_MESSAGES).post_init(post_init).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_msg_callback))
    return app
