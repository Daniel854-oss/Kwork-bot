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
from agent import AgentContext, run_orders_agent, edit_offer
from ai import generate_offer, explain_project
from config import TG_BOT_TOKEN_ORDERS, TG_CHAT_ID, MIN_BUDGET, KWORK_OFFER_TYPE
from storage import (
    load_keywords, save_keywords,
    load_blacklist, save_blacklist, is_blacklisted,
    load_seen, save_seen,
    add_training_offer, load_training_data,
)

log = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")
BUILD_VERSION = "2026-05-07-v5"

# In-memory storage for pending projects
pending_projects: dict[int, dict] = {}

# Global account manager
account_mgr: AccountManager | None = None

# Stats
stats = {"polls": 0, "offers_sent": 0, "errors": 0, "started_at": None}

# Agent context (per-chat)
agent_ctx = AgentContext()

# Global app reference for agent actions
_app: Application | None = None

# Polling control
polling_paused = False


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
    if not account_mgr or not account_mgr.accounts:
        log.error("No accounts loaded — skipping poll")
        await app.bot.send_message(TG_CHAT_ID, "❗ Нет аккаунтов! Проверь KWORK_LOGIN/PASSWORD в env.")
        return

    seen = load_seen()
    keywords = [k.lower() for k in load_keywords()]
    if not keywords:
        log.warning("No keywords — skipping poll")
        return

    new_seen = set()
    total_found = 0
    total_matched = 0
    errors = []

    for acc in account_mgr.accounts:
        try:
            async with acc.create_api() as api:
                projects = []
                for page in range(1, 6):  # up to 5 pages (~60 projects)
                    page_projects = await api.get_projects(categories_ids=["all"], page=page)
                    if not page_projects:
                        break
                    projects.extend(page_projects)
                    if len(page_projects) < 12:  # last page
                        break
            log.info("Account %s: fetched %d projects", acc.name, len(projects))
        except Exception as e:
            log.error("Error polling %s: %s", acc.name, e)
            stats["errors"] += 1
            errors.append(f"{acc.name}: {str(e)[:100]}")
            continue

        for p in projects:
            pid = getattr(p, "id", None)
            if pid is None:
                continue
            new_seen.add(pid)
            total_found += 1
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

            total_matched += 1
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
    # Cap seen_ids to prevent infinite growth
    if len(seen) > 1000:
        seen = set(sorted(seen)[-1000:])
    save_seen(seen)
    stats["polls"] += 1

    # Report errors to chat
    if errors:
        try:
            await app.bot.send_message(
                TG_CHAT_ID,
                "⚠️ Ошибки при поллинге:\n" + "\n".join(errors)
            )
        except Exception:
            pass

    log.info("Poll #%d done: %d found, %d new matched, %d in seen, %d errors",
             stats["polls"], total_found, total_matched, len(seen), len(errors))


first_poll_done = False


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback — runs every 60 seconds automatically."""
    global first_poll_done
    if polling_paused:
        return
    try:
        await poll_kwork(context.application)
        # Report first poll results
        if not first_poll_done:
            first_poll_done = True
            kws = load_keywords()
            seen = load_seen()
            await context.bot.send_message(
                TG_CHAT_ID,
                f"✅ Первая проверка завершена!\n"
                f"🔑 Ключевых слов: {len(kws)}\n"
                f"👁 Заказов в памяти: {len(seen)}\n"
                f"📤 Карточек отправлено за цикл: {stats['polls']}"
            )
    except Exception:
        stats["errors"] += 1
        tb = traceback.format_exc()
        log.error("Polling error: %s", tb)
        try:
            await context.bot.send_message(TG_CHAT_ID, f"❗ Ошибка поллинга:\n{tb[:3000]}")
        except Exception:
            pass


# ── Cards & keyboards ────────────────────────────────────

def _html(text: str) -> str:
    """Escape special HTML characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send_project_card(app: Application, project: dict):
    pid = project["id"]
    pending_projects[pid] = project
    budget = project.get("price")
    budget_str = f"{budget}₽" if budget else "не указан"
    desc = project.get("description", "")

    # Clean HTML entities from Kwork descriptions
    desc = (desc
        .replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        .replace("&mdash;", "—").replace("&ndash;", "–")
        .replace("&laquo;", "«").replace("&raquo;", "»")
        .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&nbsp;", " ").replace("&quot;", '"')
    )

    desc_preview = _html(desc[:300] + "..." if len(desc) > 300 else desc)
    name_safe = _html(project['name'][:100])
    username = project.get("username")
    username_safe = _html(str(username)) if username else "—"
    rec_id = project.get("recommended_account", "sites")
    rec_acc = account_mgr.get(rec_id)
    rec_name = _html(rec_acc.name if rec_acc else rec_id)

    cat_emoji = "🔵" if rec_id == "sites" else "🟢"

    text = (
        f"{'━' * 20}\n"
        f"💼 <b>{name_safe}</b>\n\n"
        f"💰 Бюджет: <b>{budget_str}</b>\n"
        f"👤 Заказчик: {username_safe}\n"
        f"{cat_emoji} Рекомендация: <b>{rec_name}</b>\n"
        f"{'─' * 20}\n\n"
        f"📄 {desc_preview}\n\n"
        f"🔗 <a href=\"https://kwork.ru/projects/{pid}\">Открыть на Kwork</a>"
    )

    acc_buttons = []
    for acc in account_mgr.accounts:
        marker = " ⭐" if acc.id == rec_id else ""
        label = f"✍️ {acc.name}{marker}"
        acc_buttons.append(InlineKeyboardButton(label, callback_data=f"reply:{pid}:{acc.id}"))

    keyboard = InlineKeyboardMarkup([
        acc_buttons,
        [
            InlineKeyboardButton("💡 Объяснить", callback_data=f"explain:{pid}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{pid}"),
            InlineKeyboardButton("🚫 Блок", callback_data=f"block:{pid}"),
        ]
    ])
    await app.bot.send_message(
        chat_id=TG_CHAT_ID, text=text,
        reply_markup=keyboard, parse_mode="HTML",
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
        f"📝 <b>{_html(offer['name'])}</b>\n"
        f"🏷 Аккаунт: {_html(acc_name)}\n"
        f"💰 Цена: {price}₽ | ⏱ Срок: {days} дн.\n\n"
        f"{_html(offer['text'])}"
    )
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=offer_keyboard(pid))


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
        # Set agent context
        agent_ctx.set_project(project, acc_id)
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
            # Auto-learn: save successful offer as training example
            add_training_offer(
                order_desc=project.get("description", ""),
                offer_text=project["offer_text"],
                price=price,
                days=days,
            )
            await query.message.reply_text(
                f"✅ Отклик отправлен!\n"
                f"🏷 {acc.name}\n"
                f"💰 {price}₽ | ⏱ {days} дн."
            )
        except Exception as e:
            stats["errors"] += 1
            await query.message.reply_text(f"❗ Ошибка отправки: {e}")

    elif data.startswith("explain:"):
        pid = int(data.split(":")[1])
        project = pending_projects.get(pid)
        if not project:
            await query.message.reply_text("❗ Проект не найден.")
            return
        await query.message.reply_text("💡 Анализирую проект...")
        try:
            desc = project.get("description", project.get("name", ""))
            explanation = await explain_project(desc)
            await query.message.reply_text(
                f"💡 Разбор проекта:\n\n{explanation[:3500]}"
            )
        except Exception as e:
            await query.message.reply_text(f"❗ Ошибка: {e}")

    elif data.startswith("cancel:"):
        pid = int(data.split(":")[1])
        pending_projects.pop(pid, None)
        await query.edit_message_reply_markup(reply_markup=None)


# ── Commands ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    accs = len(account_mgr.accounts) if account_mgr else 0
    kws = load_keywords()
    paused = polling_paused
    jobs = len(context.application.job_queue.jobs()) if context.application.job_queue else 0

    await update.message.reply_text(
        f"👋 *Kwork Orders Bot — AI Agent*\n\n"
        f"🔧 *Диагностика:*\n"
        f"📦 Версия: `{BUILD_VERSION}`\n"
        f"💬 Твой chat\\_id: `{chat_id}`\n"
        f"🎯 TG\\_CHAT\\_ID: `{TG_CHAT_ID}`\n"
        f"{'✅' if chat_id == TG_CHAT_ID else '❌'} ID совпадает: {chat_id == TG_CHAT_ID}\n"
        f"👥 Аккаунтов: {accs}\n"
        f"🔑 Ключевых слов: {len(kws)}\n"
        f"⏸ На паузе: {paused}\n"
        f"⏱ Задач в очереди: {jobs}\n"
        f"🔄 Циклов: {stats['polls']}\n"
        f"❗ Ошибок: {stats['errors']}\n\n"
        f"/help — все команды",
        parse_mode="Markdown",
    )

    # Test proactive message to TG_CHAT_ID
    try:
        await context.bot.send_message(
            TG_CHAT_ID,
            f"🧪 Тест проактивного сообщения!\n"
            f"Если ты видишь это — TG_CHAT_ID работает.\n"
            f"Версия: {BUILD_VERSION}"
        )
    except Exception as e:
        await update.message.reply_text(f"❗ Ошибка отправки в TG_CHAT_ID ({TG_CHAT_ID}): {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Команды:*\n\n"
        "/status — статус и статистика\n"
        "/test — самодиагностика\n"
        "/keywords — ключевые слова\n"
        "/add /remove — управление словами\n"
        "/blacklist /unblock — чёрный список\n"
        "/accounts — информация об аккаунтах\n\n"
        "💬 *AI-агент (просто пиши текстом):*\n"
        "• \"исправь цену на 3000\" — правит отклик\n"
        "• \"сделай короче\" — сокращает\n"
        "• \"объясни заказ\" — разбирает ТЗ\n"
        "• \"проверь заказы сейчас\" — поллинг\n"
        "• \"сколько коннектов?\" — баланс\n"
        "• \"какие заказы в работе?\" — активные\n"
        "• Свой текст → станет откликом",
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

    poll_status = "⏸ на паузе (/resume)" if polling_paused else "✅ активен (каждые 60 сек)"
    await update.message.reply_text(
        f"🤖 Бот заказов\n\n"
        f"🔄 Автопоиск: {poll_status}\n"
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
    )


# ── Free text → AI Agent ──────────────────────────────────

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    if not message or message.startswith("/"):
        return

    await update.message.reply_text("🤔 Думаю...")
    try:
        result = await run_orders_agent(message, agent_ctx)
    except Exception as e:
        await update.message.reply_text(f"❗ Ошибка AI: {e}")
        return

    action = result.get("action", "none")
    params = result.get("params", {})
    response = result.get("response", "")

    if action == "edit_offer" and agent_ctx.current_offer:
        instruction = params.get("instruction", message)
        try:
            new_offer = await edit_offer(agent_ctx.current_offer, instruction)
            # Apply param overrides
            if params.get("price"):
                new_offer["price"] = params["price"]
            if params.get("days"):
                new_offer["days"] = params["days"]
            agent_ctx.set_offer(new_offer)
            # Update pending project
            if agent_ctx.current_project:
                pid = agent_ctx.current_project["id"]
                project = pending_projects.get(pid)
                if project:
                    project["offer_name"] = new_offer["name"]
                    project["offer_text"] = new_offer["text"]
                    project["offer_price"] = new_offer.get("price", 1000)
                    project["offer_days"] = new_offer.get("days", 3)
                    pending_projects[pid] = project
            text = (
                f"📝 <b>{_html(new_offer['name'])}</b>\n"
                f"💰 {new_offer.get('price','?')}₽ | ⏱ {new_offer.get('days','?')} дн.\n\n"
                f"{_html(new_offer['text'])}"
            )
            pid = agent_ctx.current_project["id"] if agent_ctx.current_project else 0
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=offer_keyboard(pid))
        except Exception as e:
            await update.message.reply_text(f"❗ Ошибка редактирования: {e}")

    elif action == "set_custom_offer" and agent_ctx.current_project:
        custom_text = params.get("text", message)
        pid = agent_ctx.current_project["id"]
        offer = {
            "name": agent_ctx.current_project.get("name", "Отклик")[:50],
            "text": custom_text,
            "price": params.get("price", 1000),
            "days": params.get("days", 3),
        }
        agent_ctx.set_offer(offer)
        project = pending_projects.get(pid)
        if project:
            project["offer_name"] = offer["name"]
            project["offer_text"] = offer["text"]
            project["offer_price"] = offer["price"]
            project["offer_days"] = offer["days"]
            pending_projects[pid] = project
        # Auto-learn: user wrote their own offer — this is the best training data!
        add_training_offer(
            order_desc=agent_ctx.current_project.get("description", ""),
            offer_text=custom_text,
            price=offer["price"],
            days=offer["days"],
        )
        data = load_training_data()
        text = (
            f"📝 <b>Ваш отклик:</b>\n"
            f"💰 {offer['price']}₽ | ⏱ {offer['days']} дн.\n\n"
            f"{_html(offer['text'])}\n\n"
            f"💾 <i>Сохранён как образец (всего: {len(data['offers'])})</i>"
        )
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=offer_keyboard(pid))

    elif action == "generate_offer" and agent_ctx.current_project:
        acc_id = params.get("account_id") or agent_ctx.selected_account or "sites"
        pid = agent_ctx.current_project["id"]
        project = pending_projects.get(pid, agent_ctx.current_project)
        project["selected_account"] = acc_id
        await update.message.reply_text("⏳ Генерирую отклик...")
        try:
            offer = await generate_offer(project["description"], acc_id)
            agent_ctx.set_offer(offer)
            project["offer_name"] = offer["name"]
            project["offer_text"] = offer["text"]
            project["offer_price"] = offer.get("price", 1000)
            project["offer_days"] = offer.get("days", 3)
            pending_projects[pid] = project
            acc = account_mgr.get(acc_id)
            acc_name = acc.name if acc else acc_id
            text = (
                f"📝 <b>{_html(offer['name'])}</b>\n"
                f"🏷 {_html(acc_name)}\n"
                f"💰 {offer.get('price','?')}₽ | ⏱ {offer.get('days','?')} дн.\n\n"
                f"{_html(offer['text'])}"
            )
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=offer_keyboard(pid))
        except Exception as e:
            await update.message.reply_text(f"❗ Ошибка генерации: {e}")

    elif action == "force_poll":
        await update.message.reply_text("🔄 Проверяю заказы...")
        try:
            await poll_kwork(context.application)
            await update.message.reply_text("✅ Проверка завершена!")
        except Exception as e:
            await update.message.reply_text(f"❗ Ошибка: {e}")

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
                    lines.append(f"{acc.name}: {len(data)} заказов в работе")
                    for o in data[:3]:
                        title = o.get("title", o.get("name", "?"))[:50] if isinstance(o, dict) else str(o)[:50]
                        lines.append(f"  • {title}")
            except Exception as e:
                lines.append(f"{acc.name}: ❌ {str(e)[:40]}")
        await update.message.reply_text("📋 *Заказы в работе:*\n\n" + "\n".join(lines), parse_mode="Markdown")

    elif action == "explain_order" and agent_ctx.current_project:
        await update.message.reply_text(response)

    elif action == "show_pending":
        if pending_projects:
            lines = []
            for pid, p in list(pending_projects.items())[:10]:
                lines.append(f"• #{pid} — {p.get('name','?')[:50]}")
            await update.message.reply_text("📋 *В очереди:*\n\n" + "\n".join(lines), parse_mode="Markdown")
        else:
            await update.message.reply_text("Очередь пуста.")

    else:
        # Default: just show AI response
        if response:
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("Не понял запрос. Попробуй переформулировать.")


# ── Training commands ──────────────────────────────────

async def cmd_train(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pull real sent offers from Kwork dialogs to build training data."""
    msg = await update.message.reply_text("🏫 Собираю твои реальные ответы из Kwork...")
    total_offers = 0
    total_replies = 0

    for acc in account_mgr.accounts:
        try:
            async with acc.create_api() as api:
                me = await api.get_me()
                my_username = getattr(me, 'username', '')
                dialogs = await api.get_all_dialogs()

                for d in dialogs[:15]:  # Last 15 dialogs
                    username = getattr(d, 'username', None)
                    if not username:
                        continue
                    try:
                        messages = await api.get_dialog_with_user(username)
                    except Exception:
                        continue

                    for m in messages:
                        from_user = getattr(m, 'from_username', '')
                        text = getattr(m, 'message', '') or ''
                        if from_user == my_username and len(text) > 20:
                            # Find what the client wrote before my reply
                            idx = messages.index(m)
                            client_msg = ''
                            for prev in reversed(messages[:idx]):
                                if getattr(prev, 'from_username', '') != my_username:
                                    client_msg = getattr(prev, 'message', '') or ''
                                    break

                            from storage import add_training_reply
                            add_training_reply(client_msg[:300], text[:500])
                            total_replies += 1
                            if total_replies >= 20:
                                break
                    if total_replies >= 20:
                        break
        except Exception as e:
            log.error("Тренировка %s: %s", acc.name, e)

    data = load_training_data()
    await msg.edit_text(
        f"🏫 *Тренировка завершена!*\n\n"
        f"💬 Ответов собрано: +{total_replies}\n"
        f"📝 Образцов откликов: {len(data.get('offers', []))}\n"
        f"💬 Образцов ответов: {len(data.get('replies', []))}\n\n"
        f"ℹ️ Отклики сохраняются автоматически при отправке.",
        parse_mode="Markdown",
    )


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save current offer as a good training example."""
    if not agent_ctx.current_offer or not agent_ctx.current_project:
        await update.message.reply_text("❗ Нет активного отклика для сохранения.")
        return
    add_training_offer(
        order_desc=agent_ctx.current_project.get("description", ""),
        offer_text=agent_ctx.current_offer.get("text", ""),
        price=agent_ctx.current_offer.get("price", 0),
        days=agent_ctx.current_offer.get("days", 0),
    )
    data = load_training_data()
    await update.message.reply_text(
        f"✅ Отклик сохранён как образец!\n"
        f"📚 Всего образцов: {len(data['offers'])} откликов, {len(data['replies'])} ответов"
    )


async def cmd_training_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show training data status."""
    data = load_training_data()
    offers = data.get("offers", [])
    replies = data.get("replies", [])
    text = (
        f"🏫 *Тренировочные данные:*\n\n"
        f"📝 Образцов откликов: {len(offers)}\n"
        f"💬 Образцов ответов: {len(replies)}\n"
    )
    if offers:
        last = offers[-1]
        text += f"\nПоследний отклик: {last['offer'][:100]}..."
    if replies:
        last = replies[-1]
        text += f"\nПоследний ответ: {last['reply'][:100]}..."
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Build & run ──────────────────────────────────────────

async def cmd_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force a single verbose poll — shows exactly what was found and filtered."""
    msg = await update.message.reply_text("🔄 Проверяю заказы...")
    seen = load_seen()
    keywords = [k.lower() for k in load_keywords()]
    report = []

    for acc in account_mgr.accounts:
        report.append(f"\n<b>{acc.name}</b>")
        try:
            async with acc.create_api() as api:
                projects = []
                for page in range(1, 6):
                    page_projects = await api.get_projects(categories_ids=["all"], page=page)
                    if not page_projects:
                        break
                    projects.extend(page_projects)
                    if len(page_projects) < 12:
                        break
            report.append(f"  📦 Получено: {len(projects)}")
        except Exception as e:
            report.append(f"  ❌ Ошибка API: {e}")
            continue

        new = already_seen = bl_filtered = budget_filtered = kw_filtered = sent = 0
        for p in projects:
            pid = getattr(p, "id", None)
            if pid is None:
                continue
            if pid in seen:
                already_seen += 1
                continue
            new += 1

            title = (getattr(p, "title", None) or getattr(p, "name", None) or "").lower()
            desc = (getattr(p, "description", None) or "").lower()
            price = getattr(p, "price", None) or getattr(p, "budget", None)

            if is_blacklisted(title, desc):
                bl_filtered += 1
                continue
            if MIN_BUDGET > 0 and price and int(price) < MIN_BUDGET:
                budget_filtered += 1
                continue
            if not any(kw in title or kw in desc for kw in keywords):
                kw_filtered += 1
                continue

            username = (
                getattr(p, "username", None) or getattr(p, "user_login", None) or
                getattr(p, "login", None) or getattr(p, "user", None)
            )
            recommended = account_mgr.match_account(title, desc)
            await send_project_card(context.application, {
                "id": pid, "name": title or f"Заказ #{pid}",
                "price": price, "description": desc or title or f"Заказ #{pid}",
                "username": str(username) if username else None,
                "recommended_account": recommended.id,
            })
            seen.add(pid)
            sent += 1

        report.append(f"  🆕 Новых: {new} | 👁 Уже видел: {already_seen}")
        report.append(f"  🚫 Блок: {bl_filtered} | 💰 Бюджет: {budget_filtered} | 🔑 Ключевые: {kw_filtered}")
        report.append(f"  ✅ Отправлено карточек: {sent}")

    save_seen(seen)
    stats["polls"] += 1
    await msg.edit_text("📊 <b>Результат проверки:</b>" + "\n".join(report), parse_mode="HTML")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause automatic polling."""
    global polling_paused
    polling_paused = True
    await update.message.reply_text("⏸ Автопоиск заказов приостановлен.\nВозобнови: /resume\nРучная проверка: /poll")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume automatic polling."""
    global polling_paused
    polling_paused = False
    await update.message.reply_text("▶️ Автопоиск возобновлён! Следующая проверка через ~60 сек.")


async def cmd_clearseen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear seen order IDs so all current orders become 'new' again."""
    save_seen(set())
    await update.message.reply_text(
        "🗑 Память просмотренных заказов очищена.\n"
        "Следующий /poll или автопроверка покажут текущие заказы заново."
    )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comprehensive debug info."""
    seen = load_seen()
    kws = load_keywords()
    now = datetime.now(MSK)
    started = stats.get("started_at")
    uptime = str(now - started).split(".")[0] if started else "N/A"

    jq_status = "N/A"
    if context.application.job_queue is not None:
        jobs = context.application.job_queue.jobs()
        jq_status = f"{len(jobs)} задач активно"
    else:
        jq_status = "❌ job_queue = None"

    text = (
        f"🔧 DEBUG INFO\n"
        f"{'─' * 25}\n"
        f"📦 Версия: {BUILD_VERSION}\n"
        f"🕐 Время: {now.strftime('%H:%M:%S')} МСК\n"
        f"⏱ Аптайм: {uptime}\n"
        f"{'─' * 25}\n"
        f"⏸ Пауза: {'ДА ⚠️' if polling_paused else 'нет'}\n"
        f"🔄 Поллингов: {stats['polls']}\n"
        f"📤 Откликов отправлено: {stats['offers_sent']}\n"
        f"❌ Ошибок: {stats['errors']}\n"
        f"{'─' * 25}\n"
        f"👁 Seen IDs: {len(seen)}\n"
        f"🔑 Keywords: {len(kws)}\n"
        f"📋 Pending: {len(pending_projects)}\n"
        f"{'─' * 25}\n"
        f"⚙️ JobQueue: {jq_status}\n"
        f"👥 Аккаунтов: {len(account_mgr.accounts) if account_mgr else 0}\n"
    )
    await update.message.reply_text(text)


async def _poll_loop(app: Application):
    """Main polling loop — runs forever as asyncio task."""
    await asyncio.sleep(10)  # first delay
    log.info("Poll loop: first check starting")
    while True:
        if not polling_paused:
            try:
                await poll_kwork(app)
            except Exception:
                stats["errors"] += 1
                tb = traceback.format_exc()
                log.error("Polling error: %s", tb)
                try:
                    await app.bot.send_message(TG_CHAT_ID, f"❗ Ошибка поллинга:\n{tb[:3000]}")
                except Exception:
                    pass
        await asyncio.sleep(60)


async def post_init(app: Application):
    global _app
    _app = app
    stats["started_at"] = datetime.now(MSK)
    now = datetime.now(MSK).strftime("%H:%M:%S")
    kws = load_keywords()
    accs = len(account_mgr.accounts) if account_mgr else 0

    # Always use asyncio loop — JobQueue is unreliable on Railway (jobs silently disappear)
    asyncio.create_task(_poll_loop(app))
    poll_method = "asyncio loop ✅"
    log.info("Polling loop started (asyncio)")

    try:
        await app.bot.send_message(
            TG_CHAT_ID,
            f"🟢 Бот запущен!\n"
            f"📦 Версия: {BUILD_VERSION}\n"
            f"🕐 Время: {now} МСК\n"
            f"👥 Аккаунтов: {accs}\n"
            f"🔑 Ключевых слов: {len(kws)}\n"
            f"🔄 Поллинг: {poll_method}\n"
            f"⏱ Первая проверка через 10 сек..."
        )
    except Exception as e:
        log.error("Failed to send startup message: %s", e)


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
    app.add_handler(CommandHandler("poll", cmd_poll))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("clearseen", cmd_clearseen))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("train", cmd_train))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("training", cmd_training_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    return app
