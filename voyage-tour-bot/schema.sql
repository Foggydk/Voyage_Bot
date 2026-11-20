-- Схема PostgreSQL для бота-магазина «Вояж-тур».
-- Идемпотентная: CREATE IF NOT EXISTS + ALTER при каждом запуске.

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
    status              TEXT NOT NULL DEFAULT 'awaiting_payment',
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

-- Финансовый кабинет: себестоимость, платежи, журнал операций.

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS cost_price NUMERIC(12, 2);

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS cost_snapshot NUMERIC(12, 2);

ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'awaiting_payment';

ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_check;
ALTER TABLE orders ADD CONSTRAINT orders_status_check
    CHECK (status IN (
        'new',
        'awaiting_payment',
        'paid',
        'confirmed',
        'in_progress',
        'completed',
        'cancelled',
        'refunded',
        'expired',
        'payment_conflict'
    ));

CREATE TABLE IF NOT EXISTS payments (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            BIGINT NOT NULL REFERENCES orders (id),
    amount_kopecks      BIGINT NOT NULL CHECK (amount_kopecks > 0),
    currency            TEXT NOT NULL DEFAULT 'RUB',
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'pending',
                            'succeeded',
                            'canceled',
                            'refund_pending',
                            'refund_succeeded'
                        )),
    provider            TEXT NOT NULL DEFAULT 'simulator',
    provider_payment_id TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments (order_id);

CREATE TABLE IF NOT EXISTS finance_operations (
    id                  BIGSERIAL PRIMARY KEY,
    type                TEXT NOT NULL CHECK (type IN ('receipt', 'refund', 'expense')),
    amount_kopecks      BIGINT NOT NULL CHECK (amount_kopecks > 0),
    currency            TEXT NOT NULL DEFAULT 'RUB',
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    order_id            BIGINT REFERENCES orders (id),
    payment_id          BIGINT REFERENCES payments (id),
    category            TEXT,
    description         TEXT,
    author_telegram_id  BIGINT,
    external_event_id   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_finance_receipt_order
    ON finance_operations (order_id)
    WHERE type = 'receipt' AND order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_finance_refund_order
    ON finance_operations (order_id)
    WHERE type = 'refund' AND order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_finance_occurred_at ON finance_operations (occurred_at);
CREATE INDEX IF NOT EXISTS idx_finance_type ON finance_operations (type);

-- Снимок себестоимости события не меняется при обновлении каталога.
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS cost_estimated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE finance_operations ADD COLUMN IF NOT EXISTS cost_kopecks BIGINT NOT NULL DEFAULT 0
    CHECK (cost_kopecks >= 0);
ALTER TABLE finance_operations ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ;
ALTER TABLE finance_operations ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_finance_external_event
    ON finance_operations (external_event_id) WHERE external_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_payment
    ON payments (order_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS expense_revisions (
    id BIGSERIAL PRIMARY KEY,
    operation_id BIGINT NOT NULL REFERENCES finance_operations (id),
    action TEXT NOT NULL CHECK (action IN ('created', 'updated', 'canceled')),
    snapshot JSONB NOT NULL,
    author_telegram_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id BIGSERIAL PRIMARY KEY,
    operation_id BIGINT NOT NULL REFERENCES finance_operations (id),
    chat_id BIGINT NOT NULL,
    text TEXT NOT NULL,
    sent_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (operation_id, chat_id)
);

-- Старые неоплаченные заявки продолжают тот же сценарий магазина.
UPDATE orders SET status = 'awaiting_payment' WHERE status = 'new';

CREATE TABLE IF NOT EXISTS stock_balances (
    product_id BIGINT PRIMARY KEY REFERENCES products(id),
    on_hand INTEGER NOT NULL DEFAULT 0 CHECK(on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0 AND reserved <= on_hand)
);
INSERT INTO stock_balances(product_id) SELECT id FROM products ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS reservations (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL UNIQUE REFERENCES orders(id),
    status TEXT NOT NULL CHECK(status IN ('active','consumed','released')),
    expires_at TIMESTAMPTZ NOT NULL,
    release_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_active_reservation_expiry ON reservations(expires_at) WHERE status='active';
CREATE TABLE IF NOT EXISTS reservation_items (
    reservation_id BIGINT NOT NULL REFERENCES reservations(id),
    product_id BIGINT NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    PRIMARY KEY(reservation_id,product_id)
);
CREATE TABLE IF NOT EXISTS inventory_movements (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id),
    type TEXT NOT NULL CHECK(type IN ('supply','reserve','sale','release','return')),
    delta_on_hand INTEGER NOT NULL,
    delta_reserved INTEGER NOT NULL,
    order_id BIGINT REFERENCES orders(id),
    event_key TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS checkout_requests (
    telegram_id BIGINT NOT NULL,
    request_key UUID NOT NULL,
    fingerprint TEXT NOT NULL,
    payload JSONB NOT NULL,
    order_id BIGINT REFERENCES orders(id),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(telegram_id,request_key)
);
CREATE TABLE IF NOT EXISTS refund_jobs (
    id BIGSERIAL PRIMARY KEY,
    payment_id BIGINT NOT NULL UNIQUE REFERENCES payments(id),
    operation_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','succeeded')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Имитатор хранит подтверждение отдельно от локального журнала: потеря ответа безопасна.
CREATE TABLE IF NOT EXISTS simulator_refunds (
    operation_key TEXT PRIMARY KEY,
    payment_id BIGINT NOT NULL UNIQUE REFERENCES payments(id),
    provider_refund_id TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS simulator_faults (
    payment_id BIGINT PRIMARY KEY REFERENCES payments(id),
    failures_left INTEGER NOT NULL DEFAULT 0 CHECK(failures_left >= 0),
    lost_responses_left INTEGER NOT NULL DEFAULT 0 CHECK(lost_responses_left >= 0)
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
