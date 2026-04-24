import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime

import httpx
import pytz
from google import genai as google_genai
from dotenv import load_dotenv
from kwork import Kwork
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

KWORK_LOGIN = os.getenv("KWORK_LOGIN")
KWORK_PASSWORD = os.getenv("KWORK_PASSWORD")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
KWORK_PRICE = int(os.getenv("KWORK_PRICE", 1000))
KWORK_DURATION = int(os.getenv("KWORK_DURATION", 3))
KWORK_OFFER_TYPE = os.getenv("KWORK_OFFER_TYPE", "custom")
MIN_BUDGET = int(os.getenv("MIN_BUDGET", 0))

# Несколько API ключей через запятую: OPENROUTER_API_KEY=key1,key2,key3
OPENROUTER_KEYS = [k.strip() for k in (OPENROUTER_API_KEY or "").split(",") if k.strip()]
_current_key_index = 0


def get_next_api_key() -> str:
    global _current_key_index
    _current_key_index = (_current_key_index + 1) % len(OPENROUTER_KEYS)
    return OPENROUTER_KEYS[_current_key_index]


def get_current_api_key() -> str:
    return OPENROUTER_KEYS[_current_key_index] if OPENROUTER_KEYS else ""

KEYWORDS_FILE = "keywords.json"
BLACKLIST_FILE = "blacklist.json"
SEEN_IDS_FILE = "seen_ids.json"
MSK = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

pending_projects: dict[int, dict] = {}

OFFER_PROMPT = """Ты — веб-разработчик-фрилансер Даниил на бирже Kwork. Пиши отклики в его стиле.

Стиль Даниила:
- НЕ обращается по имени заказчика — сразу к делу
- Коротко, без воды, сразу по делу
- Показывает что разобрался в задаче (1-2 предложения по сути)
- Называет конкретную цену и срок
- Предлагает обсудить детали в личных сообщениях
- Заканчивает: "С уважением, Даниил."
- НЕ пишет "я эксперт", "гарантирую качество", "опыт 10 лет" — это звучит как спам

Специализация: WordPress, HTML/CSS/JS, верстка по макету, Telegram-боты, парсеры, скрипты на Python, доработка сайтов.

Примеры его откликов:
---
"Здравствуйте! Ознакомился с ТЗ — объём правок большой, по большей части нужно переписать код в отдельных местах. По цене 6000р., срок 2 дня. Сайт трогать не стоит, поэтому скопирую код на локальный хостинг, всё настрою, покажу — и тогда перенесу. Если заинтересовало, напишите в личные сообщения. С уважением, Даниил."
---
"Здравствуйте, заказ небольшой, поэтому сразу к делу. За 2000р. выполню до конца дня. Напишите в лс, обговорим детали. С уважением, Даниил."
---
"Здравствуйте! Вот похожие работы: https://kwork.ru/portfolio/19085359 — WordPress + ACF. Для вашего проекта сделаю пиксель-перфект перенос с макета на WordPress, установлю на хостинг. По цене и срокам: 20.000р., 7 дней. Если предложение заинтересовало, буду рад пообщаться в личных сообщениях. С уважением, Даниил."
---

Заказ:
{description}

Ответь строго в формате JSON:
{{"name": "название до 6 слов", "text": "текст отклика"}}"""


def load_keywords() -> list[str]:
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_keywords(kws: list[str]):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(kws, f, ensure_ascii=False, indent=2)


def load_blacklist() -> list[str]:
    if not os.path.exists(BLACKLIST_FILE):
        return []
    with open(BLACKLIST_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_blacklist(bl: list[str]):
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(bl, f, ensure_ascii=False, indent=2)


def is_blacklisted(title: str, desc: str) -> bool:
    bl = [w.lower() for w in load_blacklist()]
    text = (title + " " + desc).lower()
    return any(w in text for w in bl)


def load_seen() -> set[int]:
    if not os.path.exists(SEEN_IDS_FILE):
        return set()
    with open(SEEN_IDS_FILE, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(ids: set[int]):
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


def is_work_hours() -> bool:
    now = datetime.now(MSK)
    return 8 <= now.hour < 20


async def generate_offer(description: str) -> dict:
    prompt = OFFER_PROMPT.format(description=description)
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-3.1-flash-lite-preview-06-17",
        contents=prompt,
    )
    content = response.text
    start = content.find("{")
    end = content.rfind("}") + 1
    return json.loads(content[start:end])


async def send_project_card(app: Application, project: dict):
    pid = project["id"]
    pending_projects[pid] = project
    budget = project.get("price")
    budget_str = f"{budget} руб" if budget else "не указан"
    desc = project.get("description", "")
    desc_preview = desc[:300] + "..." if len(desc) > 300 else desc
    username = project.get("username")
    username_line = f"👤 `{username}`\n" if username else ""
    text = (
        f"💼 {project['name']}\n"
        f"💰 Бюджет: {budget_str}\n"
        f"{username_line}\n"
        f"{desc_preview}\n\n"
        f"🔗 https://kwork.ru/projects/{pid}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✍️ Написать отклик", callback_data=f"reply:{pid}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{pid}"),
        ],
        [
            InlineKeyboardButton("🚫 Блокировать похожие", callback_data=f"block:{pid}"),
        ]
    ])
    await app.bot.send_message(chat_id=TG_CHAT_ID, text=text, reply_markup=keyboard, parse_mode="Markdown")


def extract_block_words(title: str, desc: str) -> list[str]:
    """Extract meaningful words from title to suggest for blacklist."""
    text = (title + " " + desc).lower()
    # Remove common stop-words
    stop = {"и", "в", "на", "с", "по", "для", "из", "от", "к", "о", "а", "но", "или",
            "не", "за", "что", "как", "это", "его", "её", "их", "мне", "мы", "вы", "он",
            "она", "они", "есть", "быть", "до", "при", "об", "под", "над", "без"}
    words = re.findall(r'\b[а-яёa-z]{4,}\b', text)
    unique = list(dict.fromkeys(w for w in words if w not in stop))
    return unique[:5]


async def poll_kwork(app: Application):
    seen = load_seen()
    try:
        async with Kwork(login=KWORK_LOGIN, password=KWORK_PASSWORD) as api:
            projects = await api.get_projects(categories_ids=["all"])
    except Exception as e:
        await app.bot.send_message(TG_CHAT_ID, f"❗ Ошибка при получении заказов: {e}")
        return

    keywords = [k.lower() for k in load_keywords()]
    new_seen = set()
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

        # Skip if blacklisted
        if is_blacklisted(title, desc):
            continue

        # Skip if below min budget
        if MIN_BUDGET > 0 and price and int(price) < MIN_BUDGET:
            continue

        if any(kw in title or kw in desc for kw in keywords):
            username = (
                getattr(p, "username", None) or
                getattr(p, "user_login", None) or
                getattr(p, "login", None) or
                getattr(p, "user", None)
            )
            await send_project_card(app, {
                "id": pid,
                "name": title or f"Заказ #{pid}",
                "price": price,
                "description": desc or title or f"Заказ #{pid}",
                "username": str(username) if username else None,
            })
    seen.update(new_seen)
    save_seen(seen)


async def polling_loop(app: Application):
    while True:
        if is_work_hours():
            try:
                await poll_kwork(app)
            except Exception:
                tb = traceback.format_exc()
                try:
                    await app.bot.send_message(TG_CHAT_ID, f"❗ Необработанная ошибка:\n{tb[:3000]}")
                except Exception:
                    pass
        await asyncio.sleep(60)


def offer_keyboard(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Отправить на Kwork", callback_data=f"send:{pid}"),
            InlineKeyboardButton("🔄 Переписать", callback_data=f"regen:{pid}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{pid}"),
        ]
    ])


async def do_generate_and_reply(query, project: dict):
    pid = project["id"]
    try:
        offer = await generate_offer(project["description"])
        project["offer_name"] = offer["name"]
        project["offer_text"] = offer["text"]
        pending_projects[pid] = project
    except Exception as e:
        await query.message.reply_text(f"❗ Ошибка генерации: {e}")
        return
    text = f"📝 *{offer['name']}*\n\n{offer['text']}"
    await query.message.reply_text(text, parse_mode="Markdown", reply_markup=offer_keyboard(pid))


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
            added = []
            for w in words:
                if w not in bl:
                    bl.append(w)
                    added.append(w)
            save_blacklist(bl)
            if added:
                await query.message.reply_text(
                    f"🚫 Заблокированы слова: {', '.join(added)}\n"
                    f"Похожие заказы больше не придут.\n"
                    f"Управление: /blacklist | /unblock слово"
                )
            else:
                await query.message.reply_text("🚫 Похожие слова уже в чёрном списке.")

    elif data.startswith("reply:"):
        pid = int(data.split(":")[1])
        project = pending_projects.get(pid)
        if not project:
            await query.edit_message_text("❗ Заказ не найден в памяти.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Генерирую отклик...")
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
        await query.message.reply_text("⏳ Отправляю отклик на Kwork...")
        try:
            async with Kwork(login=KWORK_LOGIN, password=KWORK_PASSWORD) as api:
                await api.web_login(url_to_redirect="/exchange")
                await api.web.submit_exchange_offer(
                    project_id=pid,
                    offer_type=KWORK_OFFER_TYPE,
                    description=project["offer_text"],
                    kwork_price=KWORK_PRICE,
                    kwork_duration=KWORK_DURATION,
                    kwork_name=project["offer_name"],
                )
            pending_projects.pop(pid, None)
            await query.message.reply_text("✅ Отклик отправлен!")
        except Exception as e:
            await query.message.reply_text(f"❗ Ошибка отправки: {e}")

    elif data.startswith("cancel:"):
        pid = int(data.split(":")[1])
        pending_projects.pop(pid, None)
        await query.edit_message_reply_markup(reply_markup=None)


# --- Commands ---

async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kws = load_keywords()
    await update.message.reply_text("🔑 Ключевые слова:\n" + ", ".join(kws))


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
        await update.message.reply_text("🚫 Чёрный список:\n" + ", ".join(bl))
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
    await update.message.reply_text(f"✅ Разблокировано: {', '.join(removed)}" if removed else "Не найдено в чёрном списке.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(MSK)
    active = is_work_hours()
    kws = load_keywords()
    bl = load_blacklist()
    seen = load_seen()
    status = "✅ активен" if active else "😴 пауза (вне рабочих часов)"
    await update.message.reply_text(
        f"🤖 Статус: {status}\n"
        f"🕐 Время МСК: {now.strftime('%H:%M')}\n"
        f"🔑 Ключевых слов: {len(kws)}\n"
        f"🚫 В чёрном списке: {len(bl)}\n"
        f"👁 Просмотрено заказов: {len(seen)}\n"
        f"💰 Мин. бюджет: {MIN_BUDGET} руб\n"
    )


async def post_init(app: Application):
    asyncio.create_task(polling_loop(app))


def main():
    app = Application.builder().token(TG_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
