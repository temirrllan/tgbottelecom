-- Схема базы данных для бота монтёров Казактелекома

-- Пользователи (монтёры)
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,                          -- telegram_id
    username TEXT,
    full_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Заявки
CREATE TABLE IF NOT EXISTS tickets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address TEXT NOT NULL,
    problem_description TEXT,
    work_done TEXT,
    visit_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_repeat_visit BOOLEAN NOT NULL DEFAULT FALSE,
    act_number TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_visit_date ON tickets(visit_date);
CREATE INDEX IF NOT EXISTS idx_tickets_user_date ON tickets(user_id, visit_date DESC);

-- Поля, которые извлекаются Vision из скриншота CRM-заявки.
-- ALTER ... IF NOT EXISTS делает миграцию идемпотентной.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS customer_name TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS customer_phone TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS crm_ticket_number TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS license_account TEXT;
CREATE INDEX IF NOT EXISTS idx_tickets_crm_number ON tickets(crm_ticket_number);

-- Кто создал заявку (если NULL — монтёр сам себе создал).
-- Если задан — это КРОСС, который её назначил.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS created_by_id BIGINT REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_tickets_created_by ON tickets(created_by_id);

-- Личный номер заявки у каждого пользователя (1, 2, 3...).
-- Внутренний id остаётся для FK, наружу показывается user_ticket_number.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS user_ticket_number INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_tickets_user_number
    ON tickets(user_id, user_ticket_number)
    WHERE user_ticket_number IS NOT NULL;

-- Счётчик последнего номера на каждого пользователя
CREATE TABLE IF NOT EXISTS user_ticket_counters (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_number INTEGER NOT NULL DEFAULT 0
);

-- Бэкфилл: для старых заявок без личного номера присваиваем по порядку
WITH numbered AS (
    SELECT id, user_id,
           ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) AS rn
    FROM tickets
    WHERE user_ticket_number IS NULL
)
UPDATE tickets t
SET user_ticket_number = n.rn
FROM numbered n
WHERE t.id = n.id;

-- Синхронизация счётчиков с реальностью (берём максимум)
INSERT INTO user_ticket_counters (user_id, last_number)
SELECT user_id, COALESCE(MAX(user_ticket_number), 0) FROM tickets
WHERE user_ticket_number IS NOT NULL
GROUP BY user_id
ON CONFLICT (user_id) DO UPDATE
    SET last_number = GREATEST(EXCLUDED.last_number, user_ticket_counters.last_number);

-- Материалы по заявке
CREATE TABLE IF NOT EXISTS materials (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity NUMERIC(10, 2) NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT 'шт'
);

CREATE INDEX IF NOT EXISTS idx_materials_ticket_id ON materials(ticket_id);

-- Фото, прикреплённые к заявке (file_id Telegram)
CREATE TABLE IF NOT EXISTS ticket_photos (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    file_unique_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ticket_photos_ticket_id ON ticket_photos(ticket_id);

-- Кэш геокодинга адресов (общий для всех монтёров)
CREATE TABLE IF NOT EXISTS address_geocache (
    address TEXT PRIMARY KEY,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    display_name TEXT,
    geocoded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- История переписки для контекста ИИ
CREATE TABLE IF NOT EXISTS conversation_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                             -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversation_user_created
    ON conversation_history(user_id, created_at DESC);
