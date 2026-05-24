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
