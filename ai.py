import asyncio
import json
import logging

from google import genai as google_genai

from config import GEMINI_API_KEY

log = logging.getLogger(__name__)

# ── Offer prompt (for project replies) ────────────────────

OFFER_PROMPT = """Ты — веб-разработчик-фрилансер Даниил на бирже Kwork. Пиши отклики в его стиле.

Стиль Даниила:
- НЕ обращается по имени заказчика — сразу к делу
- Коротко, без воды, сразу по делу
- Показывает что разобрался в задаче (1-2 предложения по сути)
- Называет конкретную цену и срок
- Предлагает обсудить детали в личных сообщениях
- Заканчивает: "С уважением, Даниил."
- НЕ пишет "я эксперт", "гарантирую качество", "опыт 10 лет" — это звучит как спам

Специализация аккаунта: {specialization}

Примеры его откликов:
---
"Здравствуйте! Ознакомился с ТЗ — объём правок большой, по большей части нужно переписать код в отдельных местах. По цене 6000р., срок 2 дня. Сайт трогать не стоит, поэтому скопирую код на локальный хостинг, всё настрою, покажу — и тогда перенесу. Если заинтересовало, напишите в личные сообщения. С уважением, Даниил."
---
"Здравствуйте, заказ небольшой, поэтому сразу к делу. За 2000р. выполню до конца дня. Напишите в лс, обговорим детали. С уважением, Даниил."
---

Заказ:
{description}

Ответь строго в формате JSON:
{{"name": "название до 6 слов", "text": "текст отклика"}}"""

ACCOUNT_SPECIALIZATIONS = {
    "sites": "WordPress, HTML/CSS/JS, верстка по макету, Tilda, интернет-магазины, доработка сайтов, CRM-интеграции, защита сайтов",
    "bots": "Telegram-боты, VK-боты, MAX-боты, парсеры, ИИ-ассистенты, автоматизация, Mini Apps, рассылки",
}

# ── Reply prompt (for messages from clients) ──────────────

REPLY_PROMPT = """Ты — веб-разработчик Даниил на бирже Kwork. Тебе написал заказчик.

Контекст переписки:
{context}

Последнее сообщение заказчика:
{message}

Напиши короткий, вежливый и по делу ответ. Стиль:
- Вежливо, но без подхалимства
- По существу, конкретно
- Если нужно уточнить — задай вопрос
- НЕ пиши "я эксперт", "гарантирую" и т.п.

Ответь только текстом ответа, без кавычек и пояснений."""


async def generate_offer(description: str, account_id: str = "sites") -> dict:
    """Generate an offer for a Kwork project."""
    spec = ACCOUNT_SPECIALIZATIONS.get(account_id, ACCOUNT_SPECIALIZATIONS["sites"])
    prompt = OFFER_PROMPT.format(description=description, specialization=spec)

    client = google_genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents=prompt,
    )
    content = response.text
    start = content.find("{")
    end = content.rfind("}") + 1
    return json.loads(content[start:end])


async def generate_reply(message: str, context: str = "", account_id: str = "sites") -> str:
    """Generate a reply to a client message."""
    prompt = REPLY_PROMPT.format(message=message, context=context or "Нет предыдущего контекста.")

    client = google_genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()
