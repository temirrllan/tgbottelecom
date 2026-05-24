# Telegram-бот для монтёров Казактелекома

ИИ-ассистент на базе Claude для фиксации рабочих заявок монтёров.
Монтёр пишет в свободной форме — бот понимает, извлекает данные и сохраняет.
Можно спрашивать про свои заявки на естественном языке.

## Стек
- Python 3.11+, aiogram 3.x
- Google Gemini API (`gemini-2.5-flash`) — есть бесплатный тариф
- PostgreSQL + asyncpg
- Railway (Dockerfile + railway.toml)

## Запуск локально

```bash
cp .env.example .env
# заполнить BOT_TOKEN, GEMINI_API_KEY, DATABASE_URL

pip install -r requirements.txt
python -m bot.main
```

Миграции из `migrations/init.sql` применяются автоматически при старте.

## Деплой на Railway

1. Создать проект на Railway, подключить репозиторий.
2. Добавить плагин PostgreSQL — `DATABASE_URL` подтянется автоматически.
3. В переменные окружения добавить `BOT_TOKEN` и `GEMINI_API_KEY`.
4. (Опционально) `REMINDER_HOUR_START`, `REMINDER_HOUR_END`.

Railway увидит `Dockerfile` и `railway.toml` и сам соберёт сервис.

## Структура

```
bot/
├── main.py                # точка входа, диспетчер, фоновые задачи
├── handlers/
│   ├── chat.py            # свободный чат через ИИ
│   └── commands.py        # /start /help /today /week /month /find /edit /cancel
├── services/
│   ├── ai.py              # вызов Claude, парсинг JSON-ответа
│   ├── db.py              # пул asyncpg + CRUD
│   └── formatting.py      # форматирование заявок/сводок
└── models/
    └── schemas.py         # Pydantic-модели
migrations/init.sql        # схема БД
```

## Возможности ИИ

Gemini различает 4 типа сообщений и возвращает строгий JSON
(используется `response_mime_type="application/json"`):

- **SAVE_TICKET** — сохранить заявку (адрес, проблема, что сделано, материалы, акт).
- **QUERY** — поиск/отчёты («что я делал сегодня», «сколько кабеля за месяц»).
- **EDIT_TICKET** — редактирование заявки, созданной сегодня.
- **CHAT** — обычный разговор.

История последних 10 сообщений передаётся в Gemini для контекста.

## Напоминания

Раз в час бот проверяет монтёров, не писавших более 4 часов
в рабочее время (пн–сб, 08:00–18:00), и присылает напоминание.
# tgbottelecom
