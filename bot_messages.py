"""Bot 2: Message monitoring — instant notifications when clients write."""

import asyncio
import logging
import traceback
from datetime import datetime

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from accounts import AccountManager
from agent import AgentContext, run_messages_agent, edit_reply
from ai import generate_reply
from config import TG_BOT_TOKEN_MESSAGES, TG_CHAT_ID
from storage import load_seen_msgs, save_seen_msgs

log = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")

# In-memory storage for pending message replies
pending_replies: dict[str, dict] = {}

# Global account manager
account_mgr: AccountManager | None = None

# Stats
stats = {"polls": 0, "messages_found": 0, "replies_sent": 0, "errors": 0, "started_at": None}

# Agent context
agent_ctx = AgentContext()


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
            stats["errors"] += 1
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

            if msg_time in acc_seen:
                continue

            # Get full conversation for context
            context_text = ""
            try:
                async with acc.create_api() as api:
                    messages = await api.get_dialog_with_user(username)
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
            msg_preview = last_msg[:500] if last_msg else "(пустое сообщение)"
            text = (
                f"📩 *Новое сообщение*\n"
                f"🏷 Аккаунт: {acc.name}\n"
                f"👤 {username}\n\n"
                f"💬 {msg_preview}\n"
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💡 Сгенерировать ответ", callback_data=f"mreply:{reply_key}"),
                    InlineKeyboardButton("🔗 Kwork", url=f"https://kwork.ru/dialog?user={username}"),
                ]
            ])
            await app.bot.send_message(chat_id=TG_CHAT_ID, text=text, reply_markup=keyboard, parse_mode="Markdown")

            acc_seen.add(msg_time)
            stats["messages_found"] += 1

        seen_data[acc.id] = list(acc_seen)

    save_seen_msgs(seen_data)
    stats["polls"] += 1


async def message_polling_loop(app: Application):
    while True:
        try:
            await poll_messages(app)
        except Exception:
            stats["errors"] += 1
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
        f"💬 *Ответ для {reply_data['username']}* ({reply_data['account_name']}):\n\n"
        f"{reply_text}"
    )
    await query.message.reply_text(text, reply_markup=reply_keyboard(reply_key), parse_mode="Markdown")


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
            stats["replies_sent"] += 1
            await query.message.reply_text(f"✅ Ответ отправлен от {acc.name}!")
        except Exception as e:
            stats["errors"] += 1
            await query.message.reply_text(f"❗ Ошибка отправки: {e}")

    elif data.startswith("mcancel:"):
        reply_key = data[len("mcancel:"):]
        pending_replies.pop(reply_key, None)
        await query.edit_message_reply_markup(reply_markup=None)


# ── Commands ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Kwork Messages Bot*\n\n"
        "Я слежу за входящими сообщениями от заказчиков на ВСЕХ аккаунтах Kwork "
        "и моментально уведомляю тебя.\n\n"
        "📋 *Что я умею:*\n"
        "• Проверяю сообщения каждые 30 сек\n"
        "• Мониторю ВСЕ аккаунты (🔵 Сайты + 🟢 Боты)\n"
        "• Показываю контекст переписки\n"
        "• Генерирую AI-ответы\n"
        "• Отправляю ответы прямо из Telegram\n\n"
        "Напиши /help для команд или задай вопрос текстом.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды:*\n\n"
        "/status — статус бота и статистика\n"
        "/test — самодиагностика\n"
        "/help — эта справка\n\n"
        "💬 Просто напиши вопрос — AI ответит о функциях бота.",
        parse_mode="Markdown",
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Запускаю самодиагностику...")
    results = []

    # Test accounts + dialogs
    for acc in account_mgr.accounts:
        try:
            async with acc.create_api() as api:
                dialogs = await api.get_all_dialogs()
                unread = sum(1 for d in dialogs if (getattr(d, "unread", 0) or 0) > 0)
                results.append(f"✅ {acc.name} — {len(dialogs)} диалогов, {unread} непрочитанных")
        except Exception as e:
            results.append(f"❌ {acc.name} — {str(e)[:50]}")

    # Test AI
    try:
        from ai import _call_gemini
        resp = await _call_gemini("Скажи 'OK' одним словом")
        results.append(f"\n🤖 AI: ✅ {resp[:20]}")
    except Exception as e:
        results.append(f"\n🤖 AI: ❌ {str(e)[:50]}")

    results.append(f"\n📡 Telegram: ✅")
    await msg.edit_text("🔧 *Самодиагностика:*\n\n" + "\n".join(results), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(MSK)
    seen_data = load_seen_msgs()
    total_seen = sum(len(v) for v in seen_data.values())
    uptime = ""
    if stats["started_at"]:
        delta = now - stats["started_at"]
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        uptime = f"⏱ Аптайм: {hours}ч {mins}м\n"

    await update.message.reply_text(
        f"📩 *Бот сообщений*\n\n"
        f"🕐 Время МСК: {now.strftime('%H:%M')}\n"
        f"{uptime}"
        f"👥 Аккаунтов: {len(account_mgr.accounts)}\n"
        f"📨 Обнаружено сообщений: {stats['messages_found']}\n"
        f"📤 Ответов отправлено: {stats['replies_sent']}\n"
        f"🔄 Циклов проверки: {stats['polls']}\n"
        f"❗ Ошибок: {stats['errors']}\n"
        f"⏱ Интервал: 30 сек",
        parse_mode="Markdown",
    )


# ── Free text → AI Agent ──────────────────────────────────

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    if not message or message.startswith("/"):
        return

    await update.message.reply_text("🤔 Думаю...")
    try:
        result = await run_messages_agent(message, agent_ctx)
    except Exception as e:
        await update.message.reply_text(f"❗ Ошибка AI: {e}")
        return

    action = result.get("action", "none")
    params = result.get("params", {})
    response = result.get("response", "")

    if action == "get_dialogs":
        acc_id = params.get("account_id", "sites")
        acc = account_mgr.get(acc_id)
        if not acc:
            await update.message.reply_text(f"❗ Аккаунт '{acc_id}' не найден.")
            return
        try:
            async with acc.create_api() as api:
                dialogs = await api.get_all_dialogs()
            unread = [d for d in dialogs if (getattr(d, 'unread', 0) or 0) > 0]
            lines = [f"📨 *{acc.name}* — {len(dialogs)} диалогов, {len(unread)} непрочитанных\n"]
            for d in dialogs[:10]:
                username = getattr(d, 'username', '?')
                last = (getattr(d, 'last_message', '') or '')[:60]
                is_unread = '🔴' if (getattr(d, 'unread', 0) or 0) > 0 else '⚪'
                lines.append(f"{is_unread} *{username}*: {last}")
            if len(dialogs) > 10:
                lines.append(f"\n... и ещё {len(dialogs) - 10}")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❗ Ошибка: {e}")

    elif action == "get_dialog_with_user":
        username = params.get("username", "")
        acc_id = params.get("account_id", "sites")
        if not username:
            await update.message.reply_text("❗ Укажи юзернейм пользователя.")
            return
        acc = account_mgr.get(acc_id)
        if not acc:
            await update.message.reply_text(f"❗ Аккаунт '{acc_id}' не найден.")
            return
        try:
            async with acc.create_api() as api:
                messages = await api.get_dialog_with_user(username)
            agent_ctx.current_dialog_user = username
            recent = messages[-10:] if len(messages) > 10 else messages
            lines = [f"💬 *Переписка с {username}* ({acc.name})\n"]
            for m in recent:
                sender = getattr(m, 'from_username', '?')
                text = (getattr(m, 'message', '') or '')[:200]
                lines.append(f"👤 *{sender}*: {text}")
            await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❗ Ошибка: {e}")

    elif action == "get_unread":
        lines = []
        for acc in account_mgr.accounts:
            try:
                async with acc.create_api() as api:
                    dialogs = await api.get_all_dialogs()
                unread = [d for d in dialogs if (getattr(d, 'unread', 0) or 0) > 0]
                lines.append(f"{acc.name}: {len(unread)} непрочитанных")
                for d in unread[:5]:
                    username = getattr(d, 'username', '?')
                    last = (getattr(d, 'last_message', '') or '')[:50]
                    lines.append(f"  🔴 {username}: {last}")
            except Exception as e:
                lines.append(f"{acc.name}: ❌ {str(e)[:40]}")
        await update.message.reply_text("📩 *Непрочитанные:*\n\n" + "\n".join(lines), parse_mode="Markdown")

    elif action == "generate_reply":
        reply_key = None
        reply_data = None
        # Find the most recent pending reply
        for rk, rd in pending_replies.items():
            reply_key = rk
            reply_data = rd
        if reply_data:
            await update.message.reply_text("⏳ Генерирую ответ...")
            try:
                reply_text = await generate_reply(
                    message=reply_data["last_message"],
                    context=reply_data.get("context", ""),
                    account_id=reply_data["account_id"],
                )
                reply_data["generated_reply"] = reply_text
                pending_replies[reply_key] = reply_data
                text = f"💬 *Ответ для {reply_data['username']}*:\n\n{reply_text}"
                await update.message.reply_text(text, reply_markup=reply_keyboard(reply_key), parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❗ Ошибка: {e}")
        else:
            await update.message.reply_text(response or "Нет активных диалогов для ответа.")

    elif action == "edit_reply":
        # Find reply with generated text
        for rk, rd in pending_replies.items():
            if rd.get("generated_reply"):
                instruction = params.get("instruction", message)
                try:
                    new_text = await edit_reply(rd["generated_reply"], instruction, rd.get("context", ""))
                    rd["generated_reply"] = new_text
                    pending_replies[rk] = rd
                    text = f"💬 *Ответ для {rd['username']}*:\n\n{new_text}"
                    await update.message.reply_text(text, reply_markup=reply_keyboard(rk), parse_mode="Markdown")
                except Exception as e:
                    await update.message.reply_text(f"❗ Ошибка: {e}")
                break
        else:
            await update.message.reply_text("Нет сгенерированного ответа для редактирования.")

    elif action == "set_custom_reply":
        custom_text = params.get("text", message)
        # Find active dialog
        for rk, rd in pending_replies.items():
            rd["generated_reply"] = custom_text
            pending_replies[rk] = rd
            text = f"💬 *Ваш ответ для {rd['username']}*:\n\n{custom_text}"
            await update.message.reply_text(text, reply_markup=reply_keyboard(rk), parse_mode="Markdown")
            break
        else:
            await update.message.reply_text(response or "Нет активного диалога. Сначала выбери сообщение.")

    elif action == "get_connects":
        lines = []
        for acc in account_mgr.accounts:
            try:
                async with acc.create_api() as api:
                    connects = await api.get_connects()
                    total = getattr(connects, "total", "?")
                    lines.append(f"{acc.name}: {total} коннектов")
            except Exception as e:
                lines.append(f"{acc.name}: ❌ {str(e)[:40]}")
        await update.message.reply_text("💰 *Коннекты:*\n\n" + "\n".join(lines), parse_mode="Markdown")

    elif action == "get_worker_orders":
        lines = []
        for acc in account_mgr.accounts:
            try:
                async with acc.create_api() as api:
                    orders = await api.get_worker_orders()
                    data = orders.get("data", []) if isinstance(orders, dict) else []
                    lines.append(f"{acc.name}: {len(data)} заказов")
            except Exception as e:
                lines.append(f"{acc.name}: ❌ {str(e)[:40]}")
        await update.message.reply_text("📋 *Заказы:*\n\n" + "\n".join(lines), parse_mode="Markdown")

    else:
        if response:
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("Не понял. Попробуй переформулировать.")


# ── Build & run ──────────────────────────────────────────

async def post_init(app: Application):
    stats["started_at"] = datetime.now(MSK)
    asyncio.create_task(message_polling_loop(app))


def build_messages_bot(mgr: AccountManager) -> Application:
    global account_mgr
    account_mgr = mgr

    app = Application.builder().token(TG_BOT_TOKEN_MESSAGES).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_msg_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    return app
