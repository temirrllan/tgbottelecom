"""Логика работы с OpenAI API для понимания сообщений монтёров."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from openai import AsyncOpenAI, RateLimitError

from bot.models.schemas import AIResponse

logger = logging.getLogger(__name__)

# Модели и эндпоинт настраиваются переменными окружения.
# Можно использовать OpenAI напрямую или совместимые провайдеры
# (OpenRouter, DeepSeek, vLLM-сервер и т.п.) через OPENAI_BASE_URL.
CHAT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", CHAT_MODEL)
WHISPER_MODEL = os.getenv("OPENAI_WHISPER_MODEL", "whisper-1")
MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "3000"))
BASE_URL = os.getenv("OPENAI_BASE_URL")  # None → официальный OpenAI

_client: Optional[AsyncOpenAI] = None


def get_client() -> AsyncOpenAI:
    """Ленивая инициализация клиента OpenAI (или совместимого провайдера)."""
    global _client
    if _client is None:
        kwargs: dict = {"api_key": os.environ["OPENAI_API_KEY"]}
        if BASE_URL:
            kwargs["base_url"] = BASE_URL
        _client = AsyncOpenAI(**kwargs)
    return _client


def _is_rate_limit_error(err: Exception) -> bool:
    """OpenAI бросает RateLimitError при достижении лимитов."""
    return isinstance(err, RateLimitError)


SYSTEM_PROMPT = """Ты — ИИ-ассистент бота АО «Казактелеком» для бригады.
Помогаешь сотрудникам фиксировать и распределять заявки.

В системе есть две роли:
- КРОСС (диспетчер): принимает звонки от абонентов и СОЗДАЁТ заявки для назначения монтёрам.
  У КРОСС work_done всегда пустой — она не выезжает.
- Монтёр: принимает назначенные заявки, выезжает, закрывает их (заполняет work_done, materials, act_number).

Текущая роль пользователя указана отдельно в системном промпте ниже.

Общайся на русском языке, дружелюбно и кратко (без лишней воды).
Текущая дата и время передаются в каждом сообщении.

Ты должен определить одно из четырёх действий:

1. SAVE_TICKET — пользователь описывает заявку (новую или уже выполненную работу).
   Извлеки из текста:
   - address (адрес — ОБЯЗАТЕЛЬНО; если адреса нет в тексте — верни action=CHAT и попроси адрес)
   - problem_description (описание проблемы абонента — если упомянуто)
   - work_done (что СДЕЛАЛ монтёр — ТОЛЬКО если ЯВНО написал.
     Если пользователь только описал проблему и адрес — оставь null и НЕ ПЕРЕСПРАШИВАЙ.
     Для КРОСС это поле ВСЕГДА null.)
   - visit_date (ISO 8601, например "2026-05-23T14:30:00"; если указано время — используй его с текущей датой; иначе оставь null — подставит сервер)
   - is_repeat_visit (true, если в тексте есть «повторно», «снова», «опять», «второй раз»; иначе false)
   - act_number (номер акта, если назван — обычно монтёром после закрытия)
   - customer_name (ФИО абонента — если упомянуто, обычно КРОСС)
   - customer_phone (телефон абонента — только цифры)
   - materials: список объектов {name, quantity, unit}
     - name: «кабель», «патчкорд», «наконечник», «розетка» и т. п.
     - quantity: число
     - unit: «м» для кабеля, «шт» для штучного

   ВАЖНО: если есть адрес — это всегда SAVE_TICKET, даже если описана ТОЛЬКО проблема без действий.
   Не предлагай пользователю описать «что было сделано» — это сделает монтёр позже через EDIT_TICKET.

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
    open_tickets: Optional[list[dict]] = None,
    user_role: str = "монтёр",
) -> AIResponse:
    """
    Отправляет сообщение в OpenAI и возвращает структурированный ответ.
    history — последние сообщения формата [{"role": "user"|"assistant", "content": ...}].
    open_tickets — список открытых заявок текущего пользователя для контекста.
    user_role — "монтёр" или "КРОСС"; влияет на интерпретацию SAVE_TICKET.
    """
    if now is None:
        now = datetime.now().astimezone()
    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Текущие дата и время: {now.strftime('%Y-%m-%d %H:%M')} "
        f"({_weekday_ru(now)}).\n"
        f"Роль текущего пользователя: <b>{user_role}</b>."
    )
    if user_role == "КРОСС":
        system += (
            "\n\nПомни: КРОСС создаёт заявку для назначения монтёру. "
            "В SAVE_TICKET work_done всегда null. "
            "Не спрашивай у КРОСС, «что было сделано». "
            "Если есть только адрес и описание проблемы — этого достаточно для SAVE_TICKET."
        )
    if open_tickets:
        system += "\n\n" + _format_open_tickets_context(open_tickets)

    cleaned = _sanitize_history(history)
    messages: list[dict] = [{"role": "system", "content": system}]
    for msg in cleaned:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_text})

    client = get_client()
    try:
        response = await client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
            temperature=0.3,
        )
    except Exception as err:
        if _is_rate_limit_error(err):
            logger.warning("OpenAI API rate limit достигнут")
            return AIResponse(
                action="CHAT",
                data={},
                reply=(
                    "🚦 ИИ временно перегружен. Попробуй ещё раз через минуту."
                ),
            )
        logger.exception("Ошибка обращения к OpenAI API")
        return AIResponse(
            action="CHAT",
            data={},
            reply="Извини, у меня сейчас проблемы со связью. Попробуй ещё раз через минуту.",
        )

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return AIResponse(
            action="CHAT",
            data={},
            reply="Не понял запрос, можешь переформулировать?",
        )
    return _parse_response(raw)


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
        response = await client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": merge_system},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
            temperature=0.2,
        )
    except Exception as err:
        if _is_rate_limit_error(err):
            logger.warning("OpenAI API rate limit при merge_ticket")
        else:
            logger.exception("Ошибка merge_ticket")
        return pending

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return pending
    try:
        merged = json.loads(raw)
        if isinstance(merged, dict) and "address" in merged:
            return merged
        return pending
    except json.JSONDecodeError:
        logger.warning("Не удалось распарсить merge_ticket: %s", raw[:200])
        return pending


async def transcribe_voice(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
) -> Optional[str]:
    """
    Транскрибирует голосовое сообщение через OpenAI Whisper.
    Telegram отдаёт voice в OGG/Opus — Whisper это поддерживает.
    Возвращает распознанный текст или None при ошибке.
    """
    client = get_client()

    # Whisper SDK принимает файл — обёртываем bytes в BytesIO с именем
    buf = io.BytesIO(audio_bytes)
    buf.name = "voice.ogg"  # расширение важно для определения формата

    try:
        response = await client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=buf,
            language="ru",
        )
    except Exception as err:
        if _is_rate_limit_error(err):
            logger.warning("OpenAI rate limit при транскрипции голосового")
        else:
            # OpenRouter и некоторые провайдеры не поддерживают audio API
            logger.warning("Транскрипция недоступна (возможно, провайдер не поддерживает Whisper): %s", err)
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
    Vision-анализ через OpenAI gpt-4o-mini. Извлекает поля CRM-заявки.
    None — если на фото не CRM, или ошибка.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")

    client = get_client()
    try:
        response = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }],
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
            temperature=0.1,
        )
    except Exception as err:
        if _is_rate_limit_error(err):
            logger.warning("OpenAI rate limit при Vision")
        else:
            logger.exception("Ошибка вызова OpenAI Vision")
        return None

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Vision вернул невалидный JSON: %s", raw[:300])
        return None

    if not isinstance(data, dict) or not data.get("is_crm_ticket"):
        return None

    data.pop("is_crm_ticket", None)
    if not data.get("address"):
        logger.info("Vision не нашёл адрес на скриншоте")
        return None
    return data


def _parse_response(raw: str) -> AIResponse:
    """Парсит JSON-ответ OpenAI, мягко обрабатывая возможные обёртки."""
    text = raw.strip()

    # На случай, если модель всё же обернёт в ```json ... ```
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
        return AIResponse.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Не удалось распарсить JSON от OpenAI: %s", raw[:500])
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


def _format_open_tickets_context(tickets: list[dict]) -> str:
    """
    Описание открытых заявок монтёра — попадает в системный промпт,
    чтобы ИИ корректно различал «закрытие назначенной заявки» от «новая заявка».
    """
    lines = ["У монтёра сейчас открыты следующие заявки (он их ещё не закрыл):"]
    for t in tickets:
        line = f"- #{t['number']} {t['address']}"
        if t.get("problem"):
            line += f" — {t['problem']}"
        if t.get("from_dispatcher"):
            line += f" (от {t['from_dispatcher']}, КРОСС)"
        lines.append(line)
    lines.append("")
    lines.append(
        "Правила:\n"
        "- Если в сообщении назван номер из списка («по 5», «#5», «по номеру 5») — "
        "это EDIT_TICKET с этим ticket_id.\n"
        "- Если назван адрес или его часть, совпадающие с одной из заявок выше — "
        "это EDIT_TICKET с её номером.\n"
        "- Если назван ДРУГОЙ адрес, не из списка, — это SAVE_TICKET (новая заявка).\n"
        "- Если без явной привязки и в списке только одна заявка — "
        "это EDIT_TICKET с её номером.\n"
        "- Если без привязки и заявок несколько — action=CHAT и попроси уточнить номер."
    )
    return "\n".join(lines)


def _sanitize_history(history: list[dict]) -> list[dict]:
    """
    Готовит историю для OpenAI:
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
