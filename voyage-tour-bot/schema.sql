-- Схема PostgreSQL для бота-магазина «Вояж-тур».
-- Файл идемпотентный: можно выполнять при каждом запуске.

CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    telegram_id     BIGINT NOT NULL UNIQUE,
    username        TEXT,
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    destination     TEXT NOT NULL,
    tour_type       TEXT NOT NULL CHECK (tour_type IN ('leisure', 'shopping')),
    duration_days   INTEGER NOT NULL CHECK (duration_days > 0),
    price           NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS cart_items (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    product_id      BIGINT NOT NULL REFERENCES products (id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (user_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id                  BIGSERIAL PRIMARY KEY,
    public_number       TEXT NOT NULL UNIQUE,
    user_id             BIGINT NOT NULL REFERENCES users (id),
    status              TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN (
                            'new',
                            'confirmed',
                            'in_progress',
                            'completed',
                            'cancelled'
                        )),
    recipient_name      TEXT NOT NULL,
    pickup_method       TEXT NOT NULL
                        CHECK (pickup_method IN (
                            'email_voucher',
                            'office',
                            'airport_meet'
                        )),
    payment_note        TEXT NOT NULL DEFAULT 'pay_on_receipt',
    total_amount        NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS order_items (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id      BIGINT REFERENCES products (id),
    title_snapshot  TEXT NOT NULL,
    price_snapshot  NUMERIC(12, 2) NOT NULL CHECK (price_snapshot >= 0),
    quantity        INTEGER NOT NULL CHECK (quantity > 0)
);

CREATE TABLE IF NOT EXISTS assistant_sessions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS assistant_messages (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT NOT NULL REFERENCES assistant_sessions (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders (user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_assistant_sessions_user_id ON assistant_sessions (user_id);
