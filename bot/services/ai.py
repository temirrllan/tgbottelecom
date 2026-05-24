"""Логика работы с Google Gemini API для понимания сообщений монтёров."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types

from bot.models.schemas import AIResponse

logger = logging.getLogger(__name__)

# Модель Gemini — быстрая и с щедрым бесплатным тарифом
MODEL = "gemini-2.5-flash"
MAX_TOKENS = 1500

_client: Optional[genai.Client] = None


def get_client() -> genai.Client:
    """Ленивая инициализация клиента Gemini."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


SYSTEM_PROMPT = """Ты — ИИ-ассистент монтёра АО «Казактелеком».
Помогаешь монтёру фиксировать выполненные заявки и отвечаешь на вопросы про его работу.

Общайся на русском языке, дружелюбно и кратко (без лишней воды).
Текущая дата и время передаются в каждом сообщении.

Ты должен определить одно из четырёх действий:

1. SAVE_TICKET — пользователь описывает выполненную работу/заявку.
   Извлеки из текста:
   - address (адрес — обязательно; если адреса нет в тексте — верни action=CHAT и попроси адрес)
   - problem_description (описание проблемы)
   - work_done (что было сделано)
   - visit_date (ISO 8601, например "2026-05-23T14:30:00"; если указано время — используй его с текущей датой; иначе оставь null — подставит сервер)
   - is_repeat_visit (true, если в тексте есть «повторно», «снова», «опять», «второй раз»; иначе false)
   - act_number (номер акта, если назван)
   - materials: список объектов {name, quantity, unit}
     - name: «кабель», «патчкорд», «наконечник», «розетка» и т. п.
     - quantity: число
     - unit: «м» для кабеля, «шт» для штучного

2. QUERY — пользователь спрашивает про свои заявки/материалы.
   Извлеки параметры запроса в data:
   {
     "query_type": "list_tickets" | "materials_summary" | "search_address" | "last_tickets",
     "period": "today" | "week" | "month" | null,
     "address": "строка поиска или null",
     "limit": число или null
   }
   Примеры:
   - «что я делал сегодня» → query_type=list_tickets, period=today
   - «сколько кабеля за месяц» → query_type=materials_summary, period=month
   - «найди Абая 5» → query_type=search_address, address="Абая 5"
   - «последние заявки» → query_type=last_tickets, limit=5

3. EDIT_TICKET — пользователь хочет изменить заявку (только созданную сегодня).
   data: {
     "ticket_id": число или null (если null — последняя сегодняшняя),
     "changes": { поля, которые нужно поменять, формат как в SAVE_TICKET }
   }

4. CHAT — обычный разговор, приветствие, вопрос не про заявки, либо когда не хватает данных.

В поле reply пиши короткий текст ответа пользователю.
- Для SAVE_TICKET reply должно подтверждать сохранение (короткая фраза, без перечисления — данные покажет сам бот).
- Для QUERY reply пусть будет пустой строкой или коротким комментарием — сами данные выведет бот.
- Для EDIT_TICKET — подтверждение изменения.
- Для CHAT — собственно ответ.

ВАЖНО: возвращай СТРОГО валидный JSON со схемой:
{
  "action": "SAVE_TICKET" | "QUERY" | "EDIT_TICKET" | "CHAT",
  "data": { ... },
  "reply": "..."
}
"""


async def analyze_message(
    user_text: str,
    history: list[dict],
    now: Optional[datetime] = None,
) -> AIResponse:
    """
    Отправляет сообщение в Gemini и возвращает структурированный ответ.
    history — последние сообщения формата [{"role": "user"|"assistant", "content": ...}].
    """
    if now is None:
        now = datetime.now().astimezone()
    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Текущие дата и время: {now.strftime('%Y-%m-%d %H:%M')} "
        f"({_weekday_ru(now)})."
    )

    # Gemini использует роли user/model, контент в виде Content/Part.
    cleaned = _sanitize_history(history)
    contents: list[dict] = []
    for msg in cleaned:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    client = get_client()
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                max_output_tokens=MAX_TOKENS,
                temperature=0.3,
            ),
        )
    except Exception:
        logger.exception("Ошибка обращения к Gemini API")
        return AIResponse(
            action="CHAT",
            data={},
            reply="Извини, у меня сейчас проблемы со связью. Попробуй ещё раз через минуту.",
        )

    raw = (response.text or "").strip()
    if not raw:
        # Сработал safety-фильтр Gemini или пустой ответ
        logger.warning("Gemini вернул пустой ответ")
        return AIResponse(
            action="CHAT",
            data={},
            reply="Не понял запрос, можешь переформулировать?",
        )

    return _parse_response(raw)


def _parse_response(raw: str) -> AIResponse:
    """Парсит JSON-ответ Gemini, мягко обрабатывая возможные обёртки."""
    text = raw.strip()

    # На случай, если модель всё же обернёт в ```json ... ```
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
        return AIResponse.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Не удалось распарсить JSON от Gemini: %s", raw[:500])
        return AIResponse(
            action="CHAT",
            data={},
            reply=raw or "Не понял запрос, можешь переформулировать?",
        )


def _weekday_ru(dt: datetime) -> str:
    """День недели по-русски."""
    names = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    return names[dt.weekday()]


def _sanitize_history(history: list[dict]) -> list[dict]:
    """
    Готовит историю для Gemini:
    - выкидывает пустые сообщения,
    - схлопывает подряд идущие одинаковые роли,
    - гарантирует, что первая роль — 'user'.
    """
    cleaned: list[dict] = []
    for msg in history:
        content = (msg.get("content") or "").strip()
        role = msg.get("role")
        if not content or role not in ("user", "assistant"):
            continue
        if cleaned and cleaned[-1]["role"] == role:
            cleaned[-1]["content"] += "\n" + content
            continue
        cleaned.append({"role": role, "content": content})

    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned
