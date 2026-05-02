"""AI module: Gemini-based offer and reply generation with pricing intelligence."""

import asyncio
import json
import logging

from google import genai as google_genai

from config import GEMINI_API_KEY
from storage import load_training_data

log = logging.getLogger(__name__)

# ── Pricing knowledge base ────────────────────────────────

PRICING = {
    "sites": {
        "лендинг": {"min": 3000, "avg": 5000, "complex": 8000},
        "корпоративный сайт": {"min": 5000, "avg": 8000, "complex": 15000},
        "интернет-магазин": {"min": 8000, "avg": 12000, "complex": 20000},
        "правки сайта": {"min": 500, "avg": 1500, "complex": 3000},
        "правки wordpress": {"min": 500, "avg": 1500, "complex": 3000},
        "верстка по макету": {"min": 2000, "avg": 4000, "complex": 8000},
        "сайт на tilda": {"min": 2000, "avg": 3000, "complex": 5000},
        "правки tilda": {"min": 500, "avg": 1000, "complex": 2000},
        "парсер": {"min": 2000, "avg": 3000, "complex": 5000},
        "интеграция crm": {"min": 2000, "avg": 3000, "complex": 5000},
        "подключение оплаты": {"min": 1000, "avg": 2000, "complex": 3000},
        "форма обратной связи": {"min": 500, "avg": 1000, "complex": 2000},
        "ускорение сайта": {"min": 1000, "avg": 2000, "complex": 3000},
        "перенос сайта": {"min": 1000, "avg": 1500, "complex": 2000},
        "защита сайта": {"min": 2000, "avg": 3000, "complex": 5000},
    },
    "bots": {
        "telegram бот": {"min": 3000, "avg": 5000, "complex": 10000},
        "telegram бот с оплатой": {"min": 5000, "avg": 7000, "complex": 12000},
        "telegram бот запись": {"min": 3000, "avg": 5000, "complex": 8000},
        "telegram каталог": {"min": 3000, "avg": 5000, "complex": 10000},
        "telegram mini app": {"min": 5000, "avg": 8000, "complex": 15000},
        "доработка бота": {"min": 1000, "avg": 2000, "complex": 5000},
        "рассылка telegram": {"min": 2000, "avg": 3000, "complex": 5000},
        "ии бот": {"min": 3000, "avg": 5000, "complex": 10000},
        "ии ассистент": {"min": 3000, "avg": 5000, "complex": 10000},
        "vk бот": {"min": 3000, "avg": 5000, "complex": 8000},
        "max бот": {"min": 3000, "avg": 5000, "complex": 8000},
        "парсер": {"min": 2000, "avg": 3000, "complex": 5000},
        "автоматизация": {"min": 2000, "avg": 4000, "complex": 8000},
    },
}


def estimate_price(description: str, account_id: str) -> dict:
    """Estimate price range based on description keywords."""
    text = description.lower()
    prices = PRICING.get(account_id, PRICING["sites"])
    best_match = None
    best_score = 0
    for service, price_range in prices.items():
        words = service.split()
        score = sum(1 for w in words if w in text)
        if score > best_score:
            best_score = score
            best_match = price_range
    if not best_match:
        best_match = {"min": 2000, "avg": 3000, "complex": 5000}

    # Adjust for complexity signals
    complexity = 0
    complex_words = ["сложн", "срочн", "большой", "масштаб", "много", "полностью",
                     "с нуля", "под ключ", "несколько", "интеграц", "api", "crm"]
    simple_words = ["прост", "мелк", "небольш", "быстр", "одна", "мини", "легк"]
    for w in complex_words:
        if w in text:
            complexity += 1
    for w in simple_words:
        if w in text:
            complexity -= 1

    if complexity >= 2:
        return {"price": best_match["complex"], "level": "сложный"}
    elif complexity <= -1:
        return {"price": best_match["min"], "level": "простой"}
    else:
        return {"price": best_match["avg"], "level": "средний"}


# ── Dynamic examples builder ─────────────────────────────

def _build_offer_examples() -> str:
    """Build examples section from training data + fallback defaults."""
    data = load_training_data()
    examples = data.get("offers", [])

    if examples:
        # Use real examples from training data (last 5)
        lines = ["## Мои РЕАЛЬНЫЕ отклики (КОПИРУЙ стиль, длину и тон):"]
        for ex in examples[-5:]:
            lines.append(f"---\nЗаказ: {ex['order'][:100]}")
            lines.append(f"Отклик: {ex['offer']}")
            if ex.get("price"):
                lines.append(f"Цена: {ex['price']}₽, срок: {ex.get('days', '?')} дн.")
            lines.append("---")
        return "\n".join(lines)

    # Fallback: Daniil's REAL offers from Kwork
    return """## Мои РЕАЛЬНЫЕ отклики (КОПИРУЙ стиль, структуру и тон ТОЧНО):
---
Заказ: Перенос макета на WordPress
Отклик: Здравствуйте, недавно выполнял похожие по сложности работы:
1. https://kwork.ru/portfolio/19085359
2. https://kwork.ru/portfolio/19085343
Оба сайта собраны на WordPress с админкой и ACF. Весь дизайн и верстка прописаны на HTML и CSS. Для Вашего проекта, могу сделать пиксель-перфект перенос с макета на WordPress, установить на хостинг, всё настроить.
При желании могу показать админ-панель на одном из моих сайтов, чтобы Вы могли посмотреть как примерно будет выглядеть Ваша.
По цене и срокам: 20.000р., срок 7 дней.
Если предложение заинтересовало, буду рад пообщаться в личных сообщениях.
С уважением, Даниил.
---
Заказ: Интернет-магазин шин с фильтрами и импортом прайсов
Отклик: Здравствуйте!
Вот примеры моих работ:
1. https://kwork.ru/portfolio/19085359
2. https://kwork.ru/portfolio/19085343
Ваш проект понял. Реализую на WordPress + WooCommerce с умными AJAX-фильтрами под каждый тип техники (авто, мото, вело, спецтехника) — каждый со своей логикой маркировок. Импорт прайсов XLS/CSV/YML через кастомные правила разбивки атрибутов. SEO (ЧПУ, Schema-разметка, sitemap, канонические URL) входит в работу.
По деталям — готов обсудить в личных сообщениях.
С уважением, Даниил.
---

## ВАЖНО — что отличает отклики Даниила:
- Даёт ссылки на ПОРТФОЛИО (если есть релевантные работы)
- Описывает КОНКРЕТНО что сделает (не "сделаю сайт", а "WordPress + WooCommerce + AJAX-фильтры")
- Использует ТЕХНИЧЕСКИЕ термины (ACF, ЧПУ, Schema-разметка) — доказывает экспертизу
- Предлагает ПОКАЗАТЬ админ-панель — снимает страх заказчика
- Цена и срок в формате "20.000р., срок 7 дней"
- "Если предложение заинтересовало, буду рад пообщаться в личных сообщениях"
"""


def _build_reply_examples() -> str:
    """Build examples for reply generation."""
    data = load_training_data()
    examples = data.get("replies", [])

    if examples:
        lines = ["\n## Мои РЕАЛЬНЫЕ ответы (КОПИРУЙ стиль и тон):"]
        for ex in examples[-5:]:
            lines.append(f"---\nКлиент: {ex['client'][:100]}")
            lines.append(f"Мой ответ: {ex['reply']}")
            lines.append("---")
        return "\n".join(lines)

    return ""  # No examples yet — use just the style guidelines


# ── Offer prompt ──────────────────────────────────────────

OFFER_PROMPT_TEMPLATE = """Ты — веб-разработчик-фрилансер Даниил на бирже Kwork. Пиши отклик от его имени.

## Стиль Даниила — СТРОГО следуй:
- Начинает с "Здравствуйте!" или "Здравствуйте," — ВСЕГДА
- НЕ обращается по имени заказчика (кроме случаев когда имя явно указано и хочется персонализировать)
- Показывает что КОНКРЕТНО разобрался в задаче — не общие слова, а технические детали
- По возможности ссылается на портфолио: https://kwork.ru/portfolio/19085359 и https://kwork.ru/portfolio/19085343
- Описывает КАК будет делать (стек, подход, технологии)
- Может предложить показать админ-панель или демо
- Цена и срок: "По цене и срокам: X.000р., срок Y дней"
- Заканчивает: "буду рад пообщаться в личных сообщениях" или "готов обсудить в личных сообщениях"
- Подпись: "С уважением, Даниил."
- НИКОГДА не пишет: "я эксперт", "гарантирую качество", "опыт N лет", "индивидуальный подход" — это спам-клише

## Специализация аккаунта: {specialization}

## Ценообразование:
Рекомендованная цена: {price}₽ (сложность: {complexity})
Правила:
- Мелкие правки: 500-2000₽
- Средние задачи: 2000-5000₽  
- Сложные проекты: 5000-15000₽
- Крупные под ключ: 10000-25000₽
- Срок: 1-2 дня мелочи, 3-5 средние, 5-14 крупные
- Если заказчик указал бюджет — ориентируйся, но не демпингуй ниже 500₽

{examples}

## Примеры ПЛОХИХ откликов (НЕ делай так!):
- "Я опытный разработчик с 10-летним стажем..." — спам
- "Гарантирую 100% качество и индивидуальный подход" — клише
- "Готов обсудить все детали проекта" без конкретики — пустой
- Отклик без технических деталей — не вызывает доверия

## Заказ:
{description}

Ответь СТРОГО в формате JSON:
{{"name": "название до 6 слов", "text": "текст отклика", "price": число_цена_в_рублях, "days": число_срок_в_днях}}"""


ACCOUNT_SPECIALIZATIONS = {
    "sites": "WordPress, HTML/CSS/JS, верстка по макету Figma, Tilda, интернет-магазины WooCommerce/OpenCart, правки и доработка сайтов, CRM-интеграции (AmoCRM, Битрикс24), подключение оплаты (ЮКасса, Stripe), защита сайтов от взлома и DDoS",
    "bots": "Telegram-боты (каталоги, оплата, запись, рассылки, Mini Apps), VK-боты (заявки, Senler, VK Pay), MAX-боты, ИИ-ассистенты (ChatGPT, Claude), парсеры данных (Avito, WB, 2GIS), автоматизация бизнес-процессов",
}


# ── Reply prompt ──────────────────────────────────────────

REPLY_PROMPT_TEMPLATE = """Ты — веб-разработчик Даниил на бирже Kwork. Тебе написал заказчик. Напиши ответ.

## Стиль ответа:
- Вежливо, но БЕЗ подхалимства
- По существу, конкретно
- Если нужно уточнить — задай чёткий вопрос
- Если заказчик спрашивает о цене — дай конкретную цифру
- Если заказчик благодарит — коротко, "Рад помочь! Если что — обращайтесь."
- Если просит скидку — можешь предложить -10-15%, но не больше
- НИКОГДА не пиши: "я эксперт", "гарантирую", "индивидуальный подход"
- Длина: 1-4 предложения
{reply_examples}

## Контекст переписки:
{context}

## Последнее сообщение заказчика:
{message}

Ответь ТОЛЬКО текстом ответа. Без кавычек, пояснений и форматирования."""


# ── Bot help prompt ───────────────────────────────────────

BOT_HELP_PROMPT = """Ты — AI-помощник в Telegram-боте для управления фриланс-аккаунтами на Kwork.
Отвечай на вопросы о возможностях бота. Вот что он умеет:

## Бот заказов:
- Мониторит новые заказы на Kwork каждые 60 секунд
- Автоматически подбирает лучший аккаунт (🔵 Сайты или 🟢 Боты/AI)
- Генерирует отклики через AI (Gemini)
- Показывает превью перед отправкой (Отправить / Переписать / Отмена)
- Отправляет отклики напрямую на Kwork
- Команды: /status, /keywords, /add, /remove, /blacklist, /unblock, /accounts, /help, /test

## Бот сообщений:
- Мониторит входящие сообщения от заказчиков каждые 30 секунд
- Проверяет ВСЕ аккаунты Kwork
- Отправляет мгновенные уведомления
- Генерирует AI-ответы клиентам
- Отправляет ответы прямо из Telegram
- Команды: /status, /help, /test

## Аккаунты:
- 🔵 Сайты — WordPress, Tilda, верстка, интернет-магазины, CRM, защита
- 🟢 Боты/AI — Telegram/VK/MAX боты, парсеры, ИИ-ассистенты

Вопрос пользователя: {question}

Ответь коротко и по делу на русском."""


# ── Generate functions ────────────────────────────────────

async def _call_gemini(prompt: str) -> str:
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()


async def generate_offer(description: str, account_id: str = "sites") -> dict:
    """Generate an offer with intelligent pricing and training examples."""
    spec = ACCOUNT_SPECIALIZATIONS.get(account_id, ACCOUNT_SPECIALIZATIONS["sites"])
    est = estimate_price(description, account_id)
    examples = _build_offer_examples()
    prompt = OFFER_PROMPT_TEMPLATE.format(
        description=description,
        specialization=spec,
        price=est["price"],
        complexity=est["level"],
        examples=examples,
    )

    content = await _call_gemini(prompt)
    start = content.find("{")
    end = content.rfind("}") + 1
    if start == -1 or end == 0:
        return {"name": "Отклик", "text": content, "price": est["price"], "days": 3}
    result = json.loads(content[start:end])
    # Ensure price and days are present
    if "price" not in result:
        result["price"] = est["price"]
    if "days" not in result:
        result["days"] = 3
    return result


async def generate_reply(message: str, context: str = "", account_id: str = "sites") -> str:
    """Generate a reply to a client message."""
    reply_examples = _build_reply_examples()
    prompt = REPLY_PROMPT_TEMPLATE.format(
        message=message,
        context=context or "Нет предыдущего контекста.",
        reply_examples=reply_examples,
    )
    return await _call_gemini(prompt)


async def answer_question(question: str) -> str:
    """Answer a question about bot functionality."""
    prompt = BOT_HELP_PROMPT.format(question=question)
    return await _call_gemini(prompt)


# ── Explain project prompt ────────────────────────────────

EXPLAIN_PROMPT = """Ты — опытный веб-разработчик и ментор. Даниил (фрилансер) получил заказ на Kwork и хочет понять:
1. Что конкретно нужно сделать
2. Какие технологии и инструменты использовать
3. Примерный план реализации (шаги)
4. Подводные камни и на что обратить внимание
5. Реальная сложность и адекватная цена

## Заказ:
{description}

## Правила ответа:
- Пиши на русском, кратко, по делу
- Используй список/буллеты для структуры
- Если заказ простой — скажи прямо: "это просто, 1-2 дня"
- Если сложный — объясни почему и где затык
- Если заказ не по специализации (не сайты/боты) — предупреди
- Укажи конкретные технологии: WordPress + ACF, Python + aiogram, React и т.д.
- Дай оценку адекватности бюджета заказчика

Ответь структурированно."""


async def explain_project(description: str) -> str:
    """Explain a project in detail: what to do, how, tech stack, pitfalls."""
    prompt = EXPLAIN_PROMPT.format(description=description)
    return await _call_gemini(prompt)
