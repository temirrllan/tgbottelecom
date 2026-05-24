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

3. EDIT_TICKET — пользователь хочет изменить (в том числе закрыть) уже существующую заявку.
   Типичные случаи: «по 2368874 заменил кабель», «по 123 акт 555», «по последней — патчкорд 2шт».
   data: {
     "ticket_id": число или null (если null — последняя сегодняшняя),
     "changes": { поля, которые нужно поменять, формат как в SAVE_TICKET }
   }
   ticket_id — это id из бота (#123) если пользователь указал «по 123»;
   если он указал длинный номер CRM (например «по 2368874»), положи это число тоже в ticket_id —
   бот сам поищет заявку по crm_ticket_number.

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


async def transcribe_voice(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> Optional[str]:
    """
    Транскрибирует голосовое сообщение через Gemini.
    Telegram отдаёт voice в OGG/Opus, Gemini это поддерживает напрямую.
    Возвращает распознанный текст или None при ошибке.
    """
    client = get_client()
    prompt = (
        "Это голосовое сообщение монтёра Казактелекома на русском языке. "
        "Транскрибируй его точно, без комментариев и без перефразирования. "
        "Сохраняй термины (кабель, патчкорд, акт, ОНТ, оптика и т. д.). "
        "Верни ТОЛЬКО распознанный текст, ничего больше."
    )
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=[{
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": audio_bytes}},
                    {"text": prompt},
                ],
            }],
            config=types.GenerateContentConfig(
                max_output_tokens=1000,
                temperature=0.1,
            ),
        )
    except Exception:
        logger.exception("Ошибка транскрипции голосового")
        return None

    text = (response.text or "").strip()
    # Иногда модель оборачивает в кавычки — снимаем
    text = text.strip('"').strip("«»").strip()
    return text or None


VISION_PROMPT = """На вход дано изображение. Скорее всего это скриншот заявки из CRM-системы Казактелекома («Единичное повреждение», «WFM», окно заявки и т. п.).

Если это действительно скриншот CRM-заявки — извлеки данные и верни JSON:
{
  "is_crm_ticket": true,
  "address": "адрес из поля «Адрес» (улица, дом, квартира) — обязательно",
  "customer_name": "ФИО из поля «Владелец» или «Абонент»",
  "customer_phone": "телефон из «Мобильный», «Контакты» — только цифры",
  "crm_ticket_number": "номер заявки (большое число в заголовке или поле «Номер CRM»)",
  "license_account": "номер из «Лицевой счёт»",
  "problem_description": "описание проблемы — объедини текст из «Тип обращения» / «Заявлено» + комментарий оператора",
  "visit_date": "ISO 8601 или null (если на скриншоте есть дата/время заявки)",
  "is_repeat_visit": true | false
}

Если на изображении НЕ скриншот CRM-заявки (это фото работы, акта, кабеля, оборудования, обычный пейзаж) — верни:
{
  "is_crm_ticket": false
}

Возвращай строго JSON без обёрток.
"""


async def analyze_crm_photo(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> Optional[dict]:
    """
    Прогоняет изображение через Gemini Vision и пытается извлечь поля CRM-заявки.
    Возвращает словарь с распознанными полями (без флага is_crm_ticket),
    либо None — если не распознал или произошла ошибка.
    """
    client = get_client()
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=[{
                "role": "user",
                "parts": [
                    {"inline_data": {
                        "mime_type": mime_type,
                        "data": image_bytes,
                    }},
                    {"text": VISION_PROMPT},
                ],
            }],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=MAX_TOKENS,
                temperature=0.1,
            ),
        )
    except Exception:
        logger.exception("Ошибка вызова Gemini Vision")
        return None

    raw = (response.text or "").strip()
    if not raw:
        return None

    text = raw
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Vision вернул невалидный JSON: %s", raw[:300])
        return None

    if not isinstance(data, dict) or not data.get("is_crm_ticket"):
        return None

    # Возвращаем только полезные поля, без флага
    data.pop("is_crm_ticket", None)
    if not data.get("address"):
        # Адрес обязателен, без него заявку не построить
        logger.info("Vision не нашёл адрес на скриншоте")
        return None
    return data


async def merge_ticket(user_text: str, pending: dict) -> dict:
    """
    Сливает пользовательскую правку с ещё не сохранённой заявкой.
    Возвращает обновлённый словарь TicketIn (в JSON-совместимом виде).
    При ошибке возвращает исходный pending.
    """
    pending_json = json.dumps(pending, ensure_ascii=False, default=str)

    merge_system = (
        "Ты помогаешь монтёру отредактировать черновик заявки.\n"
        "Тебе дают текущий JSON заявки и текст правки от пользователя.\n"
        "Верни ОБНОВЛЁННЫЙ полный JSON заявки в том же формате:\n"
        "{\n"
        '  "address": "строка",\n'
        '  "problem_description": "строка или null",\n'
        '  "work_done": "строка или null",\n'
        '  "visit_date": "ISO 8601 или null",\n'
        '  "is_repeat_visit": true | false,\n'
        '  "act_number": "строка или null",\n'
        '  "materials": [{"name": "...", "quantity": число, "unit": "..."}]\n'
        "}\n\n"
        "Правила:\n"
        "- Новый материал из правки — добавь к существующим.\n"
        "- Материал с тем же именем — обнови количество.\n"
        "- Если правка меняет поле — замени значение.\n"
        "- Поля, которых правка не касается, оставь как было.\n"
        "- Возвращай только JSON без обёрток."
    )

    user_prompt = (
        f"Текущий черновик:\n{pending_json}\n\n"
        f"Правка от монтёра: {user_text}\n\n"
        f"Верни обновлённый JSON."
    )

    client = get_client()
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
            config=types.GenerateContentConfig(
                system_instruction=merge_system,
                response_mime_type="application/json",
                max_output_tokens=MAX_TOKENS,
                temperature=0.2,
            ),
        )
    except Exception:
        logger.exception("Ошибка merge_ticket")
        return pending

    raw = (response.text or "").strip()
    if not raw:
        return pending

    text = raw
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        merged = json.loads(text)
        if isinstance(merged, dict) and "address" in merged:
            return merged
        return pending
    except json.JSONDecodeError:
        logger.warning("Не удалось распарсить merge_ticket: %s", raw[:200])
        return pending


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
