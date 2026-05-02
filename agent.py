"""AI Agent: understands natural language and decides which Kwork API tools to use."""

import asyncio
import json
import logging
from datetime import datetime

import pytz
from google import genai as google_genai

from config import GEMINI_API_KEY

log = logging.getLogger(__name__)
MSK = pytz.timezone("Europe/Moscow")

# ── Chat memory ───────────────────────────────────────────

MAX_MEMORY = 20

class ChatMemory:
    """Stores last N messages per chat for context."""
    def __init__(self):
        self._history: list[dict] = []

    def add(self, role: str, text: str):
        self._history.append({"role": role, "text": text[:1000], "time": datetime.now(MSK).strftime("%H:%M")})
        if len(self._history) > MAX_MEMORY:
            self._history = self._history[-MAX_MEMORY:]

    def get_context(self) -> str:
        if not self._history:
            return "Нет предыдущих сообщений."
        lines = []
        for m in self._history[-10:]:
            lines.append(f"[{m['time']}] {m['role']}: {m['text']}")
        return "\n".join(lines)

    def clear(self):
        self._history.clear()


# ── Agent context ─────────────────────────────────────────

class AgentContext:
    """Tracks what the user is currently working on."""
    def __init__(self):
        self.current_project: dict | None = None      # Active project being reviewed
        self.current_offer: dict | None = None         # Generated offer (name, text, price, days)
        self.selected_account: str | None = None       # Which account is selected
        self.current_dialog_user: str | None = None    # Username of current dialog
        self.memory = ChatMemory()

    def set_project(self, project: dict, account_id: str = None):
        self.current_project = project
        self.selected_account = account_id
        self.current_offer = None

    def set_offer(self, offer: dict):
        self.current_offer = offer

    def clear(self):
        self.current_project = None
        self.current_offer = None
        self.selected_account = None
        self.current_dialog_user = None

    def summary(self) -> str:
        parts = []
        if self.current_project:
            p = self.current_project
            parts.append(f"Текущий заказ: #{p.get('id','?')} — {p.get('name','?')[:80]}")
            parts.append(f"Описание: {p.get('description','')[:300]}")
            parts.append(f"Бюджет: {p.get('price', 'не указан')}")
        if self.selected_account:
            parts.append(f"Выбран аккаунт: {self.selected_account}")
        if self.current_offer:
            o = self.current_offer
            parts.append(f"Текущий отклик: {o.get('name','')}")
            parts.append(f"Текст: {o.get('text','')}")
            parts.append(f"Цена: {o.get('price','?')}₽, срок: {o.get('days','?')} дн.")
        if self.current_dialog_user:
            parts.append(f"Текущий диалог с: {self.current_dialog_user}")
        return "\n".join(parts) if parts else "Нет активного контекста."


# ── Agent prompt ──────────────────────────────────────────

ORDERS_AGENT_PROMPT = """Ты — AI-ассистент в Telegram-боте для фриланса на Kwork. Тебя зовут Kwork Agent.
Ты помогаешь фрилансеру Даниилу управлять заказами и откликами.

## Твои возможности (действия):
- "generate_offer" — сгенерировать новый отклик на текущий заказ
- "edit_offer" — отредактировать текущий отклик (изменить цену, текст, тон, длину)
- "set_custom_offer" — использовать текст пользователя как отклик
- "explain_order" — объяснить текущий заказ простым языком
- "force_poll" — принудительно проверить новые заказы
- "show_pending" — показать заказы в очереди
- "get_connects" — проверить баланс коннектов
- "get_worker_orders" — показать активные заказы в работе
- "get_account_info" — информация об аккаунте
- "none" — просто текстовый ответ, без действия

## Контекст:
{context}

## История чата:
{history}

## Правила:
1. Если пользователь пишет текст похожий на отклик (начинается с "Здравствуйте" или содержит цену) — используй "set_custom_offer"
2. Если просит изменить/исправить/переписать отклик — используй "edit_offer"
3. Если спрашивает про заказ — используй "explain_order"
4. Если просит проверить заказы — используй "force_poll"
5. ВСЕГДА отвечай на русском
6. Для edit_offer передай instruction — что именно изменить

## Сообщение пользователя:
{message}

Ответь СТРОГО в JSON:
{{"action": "название_действия", "params": {{"instruction": "что сделать", "text": "текст если есть", "price": число_или_null, "days": число_или_null, "account_id": "sites_или_bots_или_null"}}, "response": "текст ответа пользователю"}}"""


MESSAGES_AGENT_PROMPT = """Ты — AI-ассистент в Telegram-боте для мониторинга сообщений на Kwork.
Ты помогаешь фрилансеру Даниилу читать и отвечать на сообщения заказчиков.

## Аккаунты:
- "sites" (🔵 Сайты) — сайты, WordPress, верстка
- "bots" (🟢 Боты/AI) — Telegram/VK/MAX боты, парсеры, ИИ

## Твои возможности (действия):
- "get_dialogs" — получить список диалогов на аккаунте (params.account_id)
- "get_dialog_with_user" — получить переписку с пользователем (params.username, params.account_id)
- "generate_reply" — сгенерировать ответ на сообщение
- "edit_reply" — отредактировать сгенерированный ответ
- "set_custom_reply" — использовать текст пользователя как ответ
- "get_unread" — показать непрочитанные сообщения по всем аккаунтам
- "get_connects" — проверить баланс коннектов
- "get_worker_orders" — показать активные заказы
- "none" — просто текстовый ответ

## Контекст:
{context}

## История чата:
{history}

## Правила:
1. Если пользователь просит посмотреть/показать сообщения — определи аккаунт и используй "get_dialogs" или "get_dialog_with_user"
2. Если пишет "Сайты"/"первый аккаунт" → account_id="sites", "Боты"/"второй" → account_id="bots"
3. Если просит ответить кому-то — используй "generate_reply"
4. Если пишет текст ответа сам — используй "set_custom_reply"
5. ВСЕГДА отвечай на русском

## Сообщение пользователя:
{message}

Ответь СТРОГО в JSON:
{{"action": "название_действия", "params": {{"account_id": "sites_или_bots_или_null", "username": "юзернейм_или_null", "instruction": "что сделать", "text": "текст если есть"}}, "response": "текст ответа пользователю"}}"""


EDIT_OFFER_PROMPT = """Отредактируй этот отклик на заказ по инструкции.

Текущий отклик:
Название: {name}
Текст: {text}
Цена: {price}₽
Срок: {days} дн.

Инструкция: {instruction}

Верни СТРОГО JSON:
{{"name": "название", "text": "текст отклика", "price": число, "days": число}}"""


EDIT_REPLY_PROMPT = """Отредактируй этот ответ заказчику по инструкции.

Текущий ответ: {text}

Контекст переписки: {context}

Инструкция: {instruction}

Верни только текст ответа, без кавычек."""


# ── Core functions ────────────────────────────────────────

async def _call_gemini(prompt: str) -> str:
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()


def _parse_json(text: str) -> dict:
    """Extract JSON from AI response."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return {"action": "none", "params": {}, "response": text}
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return {"action": "none", "params": {}, "response": text}


async def run_orders_agent(message: str, ctx: AgentContext) -> dict:
    """Run AI agent for orders bot. Returns {action, params, response}."""
    ctx.memory.add("user", message)
    prompt = ORDERS_AGENT_PROMPT.format(
        context=ctx.summary(),
        history=ctx.memory.get_context(),
        message=message,
    )
    raw = await _call_gemini(prompt)
    result = _parse_json(raw)
    ctx.memory.add("bot", result.get("response", ""))
    return result


async def run_messages_agent(message: str, ctx: AgentContext) -> dict:
    """Run AI agent for messages bot. Returns {action, params, response}."""
    ctx.memory.add("user", message)
    prompt = MESSAGES_AGENT_PROMPT.format(
        context=ctx.summary(),
        history=ctx.memory.get_context(),
        message=message,
    )
    raw = await _call_gemini(prompt)
    result = _parse_json(raw)
    ctx.memory.add("bot", result.get("response", ""))
    return result


async def edit_offer(current_offer: dict, instruction: str) -> dict:
    """Edit an existing offer based on natural language instruction."""
    prompt = EDIT_OFFER_PROMPT.format(
        name=current_offer.get("name", ""),
        text=current_offer.get("text", ""),
        price=current_offer.get("price", "?"),
        days=current_offer.get("days", "?"),
        instruction=instruction,
    )
    raw = await _call_gemini(prompt)
    result = _parse_json(raw)
    if "name" not in result:
        result["name"] = current_offer.get("name", "Отклик")
    if "price" not in result:
        result["price"] = current_offer.get("price", 1000)
    if "days" not in result:
        result["days"] = current_offer.get("days", 3)
    return result


async def edit_reply(current_reply: str, instruction: str, context: str = "") -> str:
    """Edit an existing reply based on instruction."""
    prompt = EDIT_REPLY_PROMPT.format(
        text=current_reply,
        context=context or "Нет контекста.",
        instruction=instruction,
    )
    return await _call_gemini(prompt)
