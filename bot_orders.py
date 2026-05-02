"""Bot 1: Order monitoring + offer generation + sending offers to Kwork."""

import asyncio
import logging
import re
import traceback
from datetime import datetime

import pytz
from kwork import Kwork
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from accounts import AccountManager
from ai import generate_offer, answer_question
from config import TG_BOT_TOKEN_ORDERS, TG_CHAT_ID, MIN_BUDGET, KWORK_OFFER_TYPE
from storage import (
    load_keywords, save_keywords,
    load_blacklist, save_blacklist, is_blacklisted,
    load_seen, save_seen,
)

log = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")

# In-memory storage for pending projects
pending_projects: dict[int, dict] = {}

# Global account manager
account_mgr: AccountManager | None = None

# Stats
stats = {"polls": 0, "offers_sent": 0, "errors": 0, "started_at": None}


def is_work_hours() -> bool:
    now = datetime.now(MSK)
    return 8 <= now.hour < 23


def extract_block_words(title: str, desc: str) -> list[str]:
    text = (title + " " + desc).lower()
    stop = {"и", "в", "на", "с", "по", "для", "из", "от", "к", "о", "а", "но", "или",
            "не", "за", "что", "как", "это", "его", "её", "их", "мне", "мы", "вы", "он",
            "она", "они", "есть", "быть", "до", "при", "об", "под", "над", "без"}
    words = re.findall(r'\b[а-яёa-z]{4,}\b', text)
    unique = list(dict.fromkeys(w for w in words if w not in stop))
    return unique[:5]


# ── Polling ───────────────────────────────────────────────

async def poll_kwork(app: Application):
    seen = load_seen()
    keywords = [k.lower() for k in load_keywords()]
    new_seen = set()

    for acc in account_mgr.accounts:
        try:
            async with acc.create_api() as api:
                projects = await api.get_projects(categories_ids=["all"])
        except Exception as e:
            log.error("Error polling %s: %s", acc.name, e)
            stats["errors"] += 1
            continue

        for p in projects:
            pid = getattr(p, "id", None)
            if pid is None:
                continue
            new_seen.add(pid)
            if pid in seen:
                continue

            title = (getattr(p, "title", None) or getattr(p, "name", None) or "").lower()
            desc = (getattr(p, "description", None) or "").lower()
            price = getattr(p, "price", None) or getattr(p, "budget", None)

            if is_blacklisted(title, desc):
                continue
            if MIN_BUDGET > 0 and price and int(price) < MIN_BUDGET:
                continue
            if not any(kw in title or kw in desc for kw in keywords):
                continue

            username = (
                getattr(p, "username", None) or
                getattr(p, "user_login", None) or
                getattr(p, "login", None) or
                getattr(p, "user", None)
            )

            # Auto-route to best account
            recommended = account_mgr.match_account(title, desc)

            await send_project_card(app, {
                "id": pid,
                "name": title or f"Заказ #{pid}",
                "price": price,
                "description": desc or title or f"Заказ #{pid}",
                "username": str(username) if username else None,
                "recommended_account": recommended.id,
            })

    seen.update(new_seen)
    save_seen(seen)
    stats["polls"] += 1


async def polling_loop(app: Application):
    while True:
        if is_work_hours():
            try:
                await poll_kwork(app)
            except Exception:
                stats["errors"] += 1
                tb = traceback.format_exc()
                try:
                    await app.bot.send_message(TG_CHAT_ID, f"❗ Ошибка:\n{tb[:3000]}")
                except Exception:
                    pass
        await asyncio.sleep(60)


# ── Cards & keyboards ────────────────────────────────────

async def send_project_card(app: Application, project: dict):
    pid = project["id"]
    pending_projects[pid] = project
    budget = project.get("price")
    budget_str = f"{budget} руб" if budget else "не указан"
    desc = project.get("description", "")
    desc_preview = desc[:300] + "..." if len(desc) > 300 else desc
    username = project.get("username")
    username_line = f"👤 `{username}`\n" if username else ""
    rec_id = project.get("recommended_account", "sites")
    rec_acc = account_mgr.get(rec_id)
    rec_name = rec_acc.name if rec_acc else rec_id

    text = (
        f"💼 *{project['name'][:100]}*\n"
        f"💰 Бюджет: {budget_str}\n"
        f"{username_line}"
        f"🏷 Рекомендован: {rec_name}\n\n"
        f"{desc_preview}\n\n"
        f"🔗 [Открыть на Kwork](https://kwork.ru/projects/{pid})"
    )

    # Build account buttons
    acc_buttons = []
    for acc in account_mgr.accounts:
        marker = " ⭐" if acc.id == rec_id else ""
        label = f"✍️ {acc.name}{marker}"
        acc_buttons.append(InlineKeyboardButton(label, callback_data=f"reply:{pid}:{acc.id}"))

    keyboard = InlineKeyboardMarkup([
        acc_buttons,
        [
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{pid}"),
            InlineKeyboardButton("🚫 Блокировать", callback_data=f"block:{pid}"),
        ]
    ])
    await app.bot.send_message(
        chat_id=TG_CHAT_ID, text=text,
        reply_markup=keyboard, parse_mode="Markdown",
        disable_web_page_preview=True,
    )


def offer_keyboard(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Отправить", callback_data=f"send:{pid}"),
            InlineKeyboardButton("🔄 Переписать", callback_data=f"regen:{pid}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{pid}"),
        ]
    ])


async def do_generate_and_reply(query, project: dict):
    pid = project["id"]
    acc_id = project.get("selected_account", "sites")
    try:
        offer = await generate_offer(project["description"], acc_id)
        project["offer_name"] = offer["name"]
        project["offer_text"] = offer["text"]
        project["offer_price"] = offer.get("price", 1000)
        project["offer_days"] = offer.get("days", 3)
        pending_projects[pid] = project
    except Exception as e:
        await query.message.reply_text(f"❗ Ошибка генерации: {e}")
        return

    acc = account_mgr.get(acc_id)
    acc_name = acc.name if acc else acc_id
    price = offer.get("price", "?")
    days = offer.get("days", "?")

    text = (
        f"📝 *{offer['name']}*\n"
        f"🏷 Аккаунт: {acc_name}\n"
        f"💰 Цена: {price}₽ | ⏱ Срок: {days} дн.\n\n"
        f"{offer['text']}"
    )
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=offer_keyboard(pid))


# ── Callback handler ─────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("skip:"):
        pid = int(data.split(":")[1])
        pending_projects.pop(pid, None)
        await query.edit_message_reply_markup(reply_markup=None)

    elif data.startswith("block:"):
        pid = int(data.split(":")[1])
        project = pending_projects.pop(pid, None)
        await query.edit_message_reply_markup(reply_markup=None)
        if project:
            words = extract_block_words(project["name"], project["description"])
            bl = load_blacklist()
            added = [w for w in words if w not in bl]
            bl.extend(added)
            save_blacklist(bl)
            if added:
                await query.message.reply_text(
                    f"🚫 Заблокированы слова: {', '.join(added)}\n"
                    f"Управление: /blacklist | /unblock слово"
                )

    elif data.startswith("reply:"):
        parts = data.split(":")
        pid = int(parts[1])
        acc_id = parts[2] if len(parts) > 2 else "sites"
        project = pending_projects.get(pid)
        if not project:
            await query.edit_message_text("❗ Заказ не найден в памяти.")
            return
        project["selected_account"] = acc_id
        pending_projects[pid] = project
        await query.edit_message_reply_markup(reply_markup=None)
        acc = account_mgr.get(acc_id)
        acc_name = acc.name if acc else acc_id
        await query.message.reply_text(f"⏳ Генерирую отклик от {acc_name}...")
        await do_generate_and_reply(query, project)

    elif data.startswith("regen:"):
        pid = int(data.split(":")[1])
        project = pending_projects.get(pid)
        if not project:
            await query.edit_message_text("❗ Заказ не найден.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Генерирую новый вариант...")
        await do_generate_and_reply(query, project)

    elif data.startswith("send:"):
        pid = int(data.split(":")[1])
        project = pending_projects.get(pid)
        if not project:
            await query.edit_message_text("❗ Заказ не найден.")
            return
        await query.edit_message_reply_markup(reply_markup=None)

        acc_id = project.get("selected_account", "sites")
        acc = account_mgr.get(acc_id)
        if not acc:
            await query.message.reply_text("❗ Аккаунт не найден.")
            return

        await query.message.reply_text(f"⏳ Отправляю отклик от {acc.name}...")
        try:
            price = project.get("offer_price", acc.price)
            days = project.get("offer_days", acc.duration)
            async with acc.create_api() as api:
                await api.web_login(url_to_redirect="/exchange")
                await api.web.submit_exchange_offer(
                    project_id=pid,
                    offer_type=KWORK_OFFER_TYPE,
                    description=project["offer_text"],
                    kwork_price=price,
                    kwork_duration=days,
                    kwork_name=project["offer_name"],
                )
            pending_projects.pop(pid, None)
            stats["offers_sent"] += 1
            await query.message.reply_text(
                f"✅ Отклик отправлен!\n"
                f"🏷 {acc.name}\n"
                f"💰 {price}₽ | ⏱ {days} дн."
            )
        except Exception as e:
            stats["errors"] += 1
            await query.message.reply_text(f"❗ Ошибка отправки: {e}")

    elif data.startswith("cancel:"):
        pid = int(data.split(":")[1])
        pending_projects.pop(pid, None)
        await query.edit_message_reply_markup(reply_markup=None)


# ── Commands ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Kwork Orders Bot*\n\n"
        "Я мониторю новые заказы на Kwork, подбираю лучший аккаунт "
        "и генерирую отклики через AI.\n\n"
        "📋 *Что я умею:*\n"
        "• Проверяю заказы каждые 60 сек\n"
        "• Автоподбор аккаунта (🔵 Сайты / 🟢 Боты)\n"
        "• AI-генерация откликов с адекватными ценами\n"
        "• Превью перед отправкой\n"
        "• Чёрный список и фильтры\n\n"
        "Напиши /help для списка команд или задай вопрос текстом.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды:*\n\n"
        "🔍 *Мониторинг:*\n"
        "/status — статус бота и статистика\n"
        "/test — самодиагностика (проверка API и аккаунтов)\n\n"
        "🔑 *Ключевые слова:*\n"
        "/keywords — текущий список\n"
        "/add слово1 слово2 — добавить\n"
        "/remove слово — удалить\n\n"
        "🚫 *Чёрный список:*\n"
        "/blacklist — список заблокированных слов\n"
        "/unblock слово — разблокировать\n\n"
        "👥 *Аккаунты:*\n"
        "/accounts — информация об аккаунтах\n\n"
        "💬 *AI:*\n"
        "Просто напиши вопрос текстом — AI ответит о функциях бота.",
        parse_mode="Markdown",
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 Запускаю самодиагностику...")
    results = []

    # Test 1: Accounts
    results.append(f"👥 Аккаунтов: {len(account_mgr.accounts)}")
    for acc in account_mgr.accounts:
        try:
            async with acc.create_api() as api:
                me = await api.get_me()
                username = getattr(me, "username", "?")
                results.append(f"  ✅ {acc.name} — @{username}")
        except Exception as e:
            results.append(f"  ❌ {acc.name} — {str(e)[:50]}")

    # Test 2: Keywords
    kws = load_keywords()
    results.append(f"\n🔑 Ключевых слов: {len(kws)}")

    # Test 3: Blacklist
    bl = load_blacklist()
    results.append(f"🚫 В чёрном списке: {len(bl)}")

    # Test 4: AI
    try:
        from ai import _call_gemini
        test_response = await _call_gemini("Скажи 'OK' одним словом")
        results.append(f"\n🤖 AI (Gemini): ✅ ответ: {test_response[:30]}")
    except Exception as e:
        results.append(f"\n🤖 AI (Gemini): ❌ {str(e)[:50]}")

    # Test 5: Telegram
    results.append(f"\n📡 Telegram: ✅ бот работает")
    results.append(f"💬 Chat ID: {TG_CHAT_ID}")

    await msg.edit_text("🔧 *Самодиагностика:*\n\n" + "\n".join(results), parse_mode="Markdown")


async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kws = load_keywords()
    await update.message.reply_text("🔑 *Ключевые слова:*\n" + ", ".join(kws), parse_mode="Markdown")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /add слово1 слово2 ...")
        return
    kws = load_keywords()
    added = [w.lower() for w in context.args if w.lower() not in kws]
    kws.extend(added)
    save_keywords(kws)
    await update.message.reply_text(f"✅ Добавлено: {', '.join(added)}" if added else "Уже есть.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /remove слово")
        return
    kws = load_keywords()
    removed = [w.lower() for w in context.args if w.lower() in kws]
    for w in removed:
        kws.remove(w)
    save_keywords(kws)
    await update.message.reply_text(f"✅ Удалено: {', '.join(removed)}" if removed else "Не найдено.")


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bl = load_blacklist()
    if bl:
        await update.message.reply_text("🚫 *Чёрный список:*\n" + ", ".join(bl), parse_mode="Markdown")
    else:
        await update.message.reply_text("Чёрный список пуст.")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /unblock слово")
        return
    bl = load_blacklist()
    removed = [w.lower() for w in context.args if w.lower() in bl]
    for w in removed:
        bl.remove(w)
    save_blacklist(bl)
    await update.message.reply_text(f"✅ Разблокировано: {', '.join(removed)}" if removed else "Не найдено.")


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for acc in account_mgr.accounts:
        lines.append(
            f"{acc.name}\n"
            f"  📊 Услуг: {len(acc.services)} ключевых слов\n"
            f"  💰 Базовая цена: {acc.price}₽\n"
            f"  ⏱ Базовый срок: {acc.duration} дн."
        )
    await update.message.reply_text("👥 *Аккаунты:*\n\n" + "\n\n".join(lines), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(MSK)
    active = is_work_hours()
    kws = load_keywords()
    bl = load_blacklist()
    seen = load_seen()
    uptime = ""
    if stats["started_at"]:
        delta = now - stats["started_at"]
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        uptime = f"⏱ Аптайм: {hours}ч {mins}м\n"

    status = "✅ активен" if active else "😴 пауза (вне рабочих часов)"
    await update.message.reply_text(
        f"🤖 *Бот заказов*\n\n"
        f"📊 Статус: {status}\n"
        f"🕐 Время МСК: {now.strftime('%H:%M')}\n"
        f"{uptime}"
        f"👥 Аккаунтов: {len(account_mgr.accounts)}\n"
        f"🔑 Ключевых слов: {len(kws)}\n"
        f"🚫 В чёрном списке: {len(bl)}\n"
        f"👁 Просмотрено заказов: {len(seen)}\n"
        f"💰 Мин. бюджет: {MIN_BUDGET} руб\n"
        f"📤 Откликов отправлено: {stats['offers_sent']}\n"
        f"🔄 Циклов проверки: {stats['polls']}\n"
        f"❗ Ошибок: {stats['errors']}",
        parse_mode="Markdown",
    )


# ── Free text → AI answer ────────────────────────────────

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    if not question or question.startswith("/"):
        return
    await update.message.reply_text("🤔 Думаю...")
    try:
        answer = await answer_question(question)
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❗ Ошибка AI: {e}")


# ── Build & run ──────────────────────────────────────────

async def post_init(app: Application):
    stats["started_at"] = datetime.now(MSK)
    asyncio.create_task(polling_loop(app))


def build_orders_bot(mgr: AccountManager) -> Application:
    global account_mgr
    account_mgr = mgr

    app = Application.builder().token(TG_BOT_TOKEN_ORDERS).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    return app
