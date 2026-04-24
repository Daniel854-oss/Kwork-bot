import asyncio
import json
import logging
import os
import traceback
from datetime import datetime

import httpx
import pytz
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
KWORK_PRICE = int(os.getenv("KWORK_PRICE", 1000))
KWORK_DURATION = int(os.getenv("KWORK_DURATION", 3))
KWORK_OFFER_TYPE = os.getenv("KWORK_OFFER_TYPE", "custom")

KEYWORDS_FILE = "keywords.json"
SEEN_IDS_FILE = "seen_ids.json"
MSK = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# In-memory state: project_id -> project data
pending_projects: dict[int, dict] = {}


def load_keywords() -> list[str]:
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_keywords(kws: list[str]):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(kws, f, ensure_ascii=False, indent=2)


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
    prompt = (
        "Ты — фрилансер на Kwork. Напиши отклик на заказ.\n"
        "Заказ:\n" + description + "\n\n"
        "Ответь строго в формате JSON с двумя полями:\n"
        '{"name": "название предложения до 7 слов", "text": "текст отклика — профессиональный, краткий, показывает понимание задачи, предлагает обсудить детали"}'
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "google/gemini-flash-1.5",
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # Extract JSON from response
        start = content.find("{")
        end = content.rfind("}") + 1
        return json.loads(content[start:end])


async def send_project_card(app: Application, project: dict):
    pid = project["id"]
    pending_projects[pid] = project
    text = (
        f"📌 {project['name']}\n"
        f"💰 Бюджет: {project.get('price', '—')} руб\n"
        f"🔗 https://kwork.ru/projects/{pid}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✍️ Написать отклик", callback_data=f"reply:{pid}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{pid}"),
        ]
    ])
    await app.bot.send_message(chat_id=TG_CHAT_ID, text=text, reply_markup=keyboard)


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
        if any(kw in title or kw in desc for kw in keywords):
            await send_project_card(app, {
                "id": pid,
                "name": title or f"Заказ #{pid}",
                "price": price,
                "description": desc or title or f"Заказ #{pid}",
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


# --- Handlers ---

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("skip:"):
        pid = int(data.split(":")[1])
        pending_projects.pop(pid, None)
        await query.edit_message_reply_markup(reply_markup=None)

    elif data.startswith("reply:"):
        pid = int(data.split(":")[1])
        project = pending_projects.get(pid)
        if not project:
            await query.edit_message_text("❗ Заказ не найден в памяти.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Генерирую отклик...")
        try:
            offer = await generate_offer(project["description"])
            project["offer_name"] = offer["name"]
            project["offer_text"] = offer["text"]
            pending_projects[pid] = project
        except Exception as e:
            await query.message.reply_text(f"❗ Ошибка генерации: {e}")
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Отправить на Kwork", callback_data=f"send:{pid}"),
                InlineKeyboardButton("🔄 Переписать", callback_data=f"regen:{pid}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{pid}"),
            ]
        ])
        text = f"📝 *Предложение:* {offer['name']}\n\n{offer['text']}"
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data.startswith("regen:"):
        pid = int(data.split(":")[1])
        project = pending_projects.get(pid)
        if not project:
            await query.edit_message_text("❗ Заказ не найден.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏳ Генерирую новый вариант...")
        try:
            offer = await generate_offer(project["description"])
            project["offer_name"] = offer["name"]
            project["offer_text"] = offer["text"]
            pending_projects[pid] = project
        except Exception as e:
            await query.message.reply_text(f"❗ Ошибка генерации: {e}")
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Отправить на Kwork", callback_data=f"send:{pid}"),
                InlineKeyboardButton("🔄 Переписать", callback_data=f"regen:{pid}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{pid}"),
            ]
        ])
        text = f"📝 *Предложение:* {offer['name']}\n\n{offer['text']}"
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

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


async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kws = load_keywords()
    await update.message.reply_text("Ключевые слова:\n" + ", ".join(kws))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /add слово1 слово2 ...")
        return
    kws = load_keywords()
    added = []
    for w in context.args:
        w = w.lower()
        if w not in kws:
            kws.append(w)
            added.append(w)
    save_keywords(kws)
    await update.message.reply_text(f"Добавлено: {', '.join(added)}" if added else "Уже есть.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /remove слово")
        return
    kws = load_keywords()
    removed = []
    for w in context.args:
        w = w.lower()
        if w in kws:
            kws.remove(w)
            removed.append(w)
    save_keywords(kws)
    await update.message.reply_text(f"Удалено: {', '.join(removed)}" if removed else "Не найдено.")


async def post_init(app: Application):
    asyncio.create_task(polling_loop(app))


def main():
    app = Application.builder().token(TG_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
