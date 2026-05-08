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

OFFER_PROMPT_TEMPLATE = """Ты — Даниил, веб-разработчик-фрилансер на Kwork. Пиши отклик от его имени.

## Стиль Даниила — как он РЕАЛЬНО пишет:
- Коротко и по делу, как живой человек. НЕ как робот.
- Начало: "Здравствуйте!" — и сразу к сути, БЕЗ "я понял вашу задачу", "готов взяться"
- Сразу показывает что ПОНЯЛ задачу — пересказ в 1 предложении своими словами
- Потом коротко — как будет делать, какие технологии, что конкретно сделает
- НЕ ПЕРЕЧИСЛЯЙ портфолио ссылками! Только если задача прям идеально совпадает с работой в портфолио — тогда ОДНУ ссылку, и то необязательно
- Цена и срок: просто написать "По цене — X руб., по срокам — Y дней." (без "000" формата, просто число)
- Финал: "Напишите, обсудим детали." или "Готов обсудить." — коротко
- Подпись: "С уважением, Даниил." (необязательно, если и без неё нормально)

## АНТИ-ПАТТЕРНЫ (НИКОГДА ТАК НЕ ПИШИ):
- ❌ "Я опытный разработчик с N-летним стажем" — спам
- ❌ "Гарантирую качество / индивидуальный подход" — клише  
- ❌ "Подобные задачи регулярно выполняю" — шаблонно
- ❌ "Вот примеры моих работ, демонстрирующие..." — канцелярит
- ❌ Нумерованные списки шагов из 5+ пунктов — перегруз
- ❌ "Готов провести полную инвентаризацию / аудит / анализ" + расписывать каждый шаг — слишком длинно
- ❌ Дублирование того что заказчик и так написал в ТЗ

## КАК ПРАВИЛЬНО (примеры стиля):
"Здравствуйте! Задача понятна — нужно настроить бекап сайта на внешнее хранилище + скрипт восстановления. Напишу bash-скрипт, который будет автоматом делать бекап БД и файлов, заливать по FTP и дублировать на облако. Восстановление с выбором версии тоже сделаю. По цене — 2000р., по срокам — 2 дня. Напишите, обсудим детали."

"Здравствуйте! Посмотрел задачу по OpenCart — видимость товаров для опта/розницы и фикс скролла при добавлении в избранное. Сделаю через кастомное поле в админке + проверку группы покупателя на фронте. Скролл поправлю. По цене — 1500р., по срокам — 2 дня. Готов обсудить."

## Специализация аккаунта: {specialization}

## Ценообразование:
Рекомендованная цена: {price}₽ (сложность: {complexity})
{price_limit_note}
Правила:
- Мелкие правки: 500-2000₽
- Средние задачи: 2000-5000₽  
- Сложные проекты: 5000-15000₽
- Крупные под ключ: 10000-25000₽
- Срок: 1-2 дня мелочи, 3-5 средние, 5-14 крупные
- Если заказчик указал бюджет — ориентируйся на него
- ВАЖНО: Цена НЕ ДОЛЖНА превышать лимит Kwork!

{examples}

## Заказ:
{description}

ВАЖНО: Текст отклика МИНИМУМ 150 символов (требование Kwork). Но не раздувай искусственно — лучше добавь конкретику.

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

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]


async def _call_gemini(prompt: str) -> str:
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    last_error = None

    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model,
                    contents=prompt,
                )
                return response.text.strip()
            except Exception as e:
                last_error = e
                err_str = str(e)
                # Retry only on 503 / overload / rate limit
                if "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    log.warning("Gemini %s attempt %d failed (503/429), retrying in %ds...", model, attempt + 1, wait)
                    await asyncio.sleep(wait)
                    continue
                raise  # Other errors — don't retry
        log.warning("All retries exhausted for %s, trying next model...", model)

    raise last_error  # All models failed


async def transcribe_voice(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe voice message audio using Gemini multimodal."""
    import base64
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    
    contents = [
        {
            "parts": [
                {"text": "Расшифруй это голосовое сообщение. Верни ТОЛЬКО текст того что сказано, без комментариев и пояснений. Язык — русский."},
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": audio_b64,
                    }
                },
            ]
        }
    ]
    
    for model in GEMINI_MODELS:
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=contents,
            )
            return response.text.strip()
        except Exception as e:
            log.warning("Transcription with %s failed: %s", model, e)
            continue
    
    raise RuntimeError("Voice transcription failed on all models")


async def generate_offer(description: str, account_id: str = "sites",
                         budget: int = 0, max_price: int = 0) -> dict:
    """Generate an offer with intelligent pricing and training examples.
    
    Args:
        budget: client's stated budget
        max_price: Kwork's possible_price_limit for this project
    """
    spec = ACCOUNT_SPECIALIZATIONS.get(account_id, ACCOUNT_SPECIALIZATIONS["sites"])
    est = estimate_price(description, account_id)
    examples = _build_offer_examples()

    # Build price limit note for AI
    price_limit_note = ""
    if max_price and max_price > 0:
        price_limit_note = f"⚠️ ЛИМИТ KWORK: максимальная цена для этого заказа — {max_price}₽. НЕ ПРЕВЫШАЙ!"
        # Also clamp our estimate
        est["price"] = min(est["price"], max_price)
    if budget and budget > 0:
        price_limit_note += f"\n💰 Бюджет заказчика: {budget}₽"

    prompt = OFFER_PROMPT_TEMPLATE.format(
        description=description,
        specialization=spec,
        price=est["price"],
        complexity=est["level"],
        examples=examples,
        price_limit_note=price_limit_note,
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
    # HARD CLAMP: never exceed Kwork's price limit
    if max_price and max_price > 0:
        result["price"] = min(int(result["price"]), max_price)
    # Minimum 500₽
    result["price"] = max(int(result["price"]), 500)
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
