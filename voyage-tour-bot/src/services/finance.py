"""Финансы из общей БД: московские периоды, точные суммы и аудит расходов."""

import json
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from src.services.payments import require_manager

MOSCOW = timezone(timedelta(hours=3))
CATEGORIES = {
    "delivery": "Трансфер / доставка",
    "documents": "Виза / документы",
    "advertising": "Реклама",
    "commission": "Комиссия",
    "other": "Другое",
}


def period_bounds(start=None, end=None):
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
        if first and last and first > last:
            raise ValueError
        return (
            datetime.combine(first, time.min, MOSCOW) if first else None,
            datetime.combine(last + timedelta(days=1), time.min, MOSCOW)
            if last
            else None,
        )
    except (ValueError, OverflowError, TypeError):
        raise ValueError(
            "Укажите корректный период: даты ГГГГ-ММ-ДД, начало не позже конца."
        ) from None


async def summary(pool, manager_id, start=None, end=None):
    require_manager(manager_id)
    first, last = period_bounds(start, end)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(amount_kopecks) FILTER (WHERE type='receipt'),0)::bigint AS receipts,
                COALESCE(SUM(amount_kopecks) FILTER (WHERE type='refund'),0)::bigint AS refunds,
                COALESCE(SUM(amount_kopecks) FILTER (WHERE type='expense'),0)::bigint AS expenses,
                COALESCE(SUM(cost_kopecks) FILTER (WHERE type='receipt'),0)::bigint AS cost,
                COALESCE(SUM(cost_kopecks) FILTER (WHERE type='refund'),0)::bigint AS returned_cost
            FROM finance_operations WHERE voided_at IS NULL
                AND ($1::timestamptz IS NULL OR occurred_at >= $1)
                AND ($2::timestamptz IS NULL OR occurred_at < $2)
        """,
            first,
            last,
        )
    result = dict(row)
    result.update(
        cash_flow=result["receipts"] - result["refunds"] - result["expenses"],
        learning_result=result["receipts"]
        - result["refunds"]
        - result["cost"]
        + result["returned_cost"]
        - result["expenses"],
        start=start,
        end=end,
        timezone="Europe/Moscow",
        currency="RUB",
    )
    return result


async def operations(pool, manager_id, start=None, end=None, *, limit=50, offset=0):
    require_manager(manager_id)
    first, last = period_bounds(start, end)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT f.*, o.public_number,
                COALESCE((SELECT json_agg(json_build_object(
                    'action',r.action,'snapshot',r.snapshot,'author_telegram_id',r.author_telegram_id,'created_at',r.created_at)
                    ORDER BY r.id) FROM expense_revisions r WHERE r.operation_id=f.id),'[]'::json) AS revisions
            FROM finance_operations f LEFT JOIN orders o ON o.id=f.order_id
            WHERE ($1::timestamptz IS NULL OR f.occurred_at >= $1)
              AND ($2::timestamptz IS NULL OR f.occurred_at < $2)
            ORDER BY f.occurred_at DESC,f.id DESC LIMIT $3 OFFSET $4
        """,
            first,
            last,
            limit,
            offset,
        )
    result = []
    for row in rows:
        item = dict(row)
        item["revisions"] = json.loads(item["revisions"])
        result.append(item)
    return result


def _expense_values(data):
    amount = data.get("amount_kopecks")
    if type(amount) is not int or not 0 < amount <= 999_999_999_999:
        raise ValueError(
            "Сумма расхода должна быть положительным целым числом копеек, не более 9 999 999 999,99 ₽."
        )
    category = data.get("category")
    if category not in CATEGORIES:
        raise ValueError("Выберите категорию операционного расхода.")
    description = data.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 300:
        raise ValueError("Описание должно содержать от 1 до 300 символов.")
    try:
        day = date.fromisoformat(data.get("date", ""))
        if day > datetime.now(MOSCOW).date():
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Укажите дату расхода не позднее сегодняшнего дня.") from None
    return (
        amount,
        category,
        description.strip(),
        datetime.combine(day, time.min, MOSCOW),
    )


async def _revision(conn, operation_id, action, manager_id):
    await conn.execute(
        """
        INSERT INTO expense_revisions (operation_id,action,snapshot,author_telegram_id)
        SELECT id,$2,to_jsonb(f),$3 FROM finance_operations f WHERE id=$1
    """,
        operation_id,
        action,
        manager_id,
    )


async def create_expense(pool, manager_id, data):
    require_manager(manager_id)
    amount, category, description, occurred = _expense_values(data)
    try:
        event = "expense:" + str(UUID(data.get("request_id", "")))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Для расхода нужен уникальный request_id (UUID).") from None
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO finance_operations
                (type,amount_kopecks,category,description,occurred_at,author_telegram_id,external_event_id)
            VALUES ('expense',$1,$2,$3,$4,$5,$6)
            ON CONFLICT (external_event_id) WHERE external_event_id IS NOT NULL DO NOTHING RETURNING *
        """,
            amount,
            category,
            description,
            occurred,
            manager_id,
            event,
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM finance_operations WHERE external_event_id=$1", event
            )
            if (
                row["amount_kopecks"],
                row["category"],
                row["description"],
                row["occurred_at"],
                row["author_telegram_id"],
            ) != (amount, category, description, occurred, manager_id):
                raise ValueError("Этот request_id уже использован для другого расхода.")
        else:
            await _revision(conn, row["id"], "created", manager_id)
        return dict(row)


async def change_expense(pool, manager_id, operation_id, data, *, cancel=False):
    require_manager(manager_id)
    values = None if cancel else _expense_values(data)
    version = data.get("version")
    if type(version) is not int or version < 1:
        raise ValueError("Обновите журнал перед изменением расхода.")
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM finance_operations WHERE id=$1 AND type='expense' FOR UPDATE",
            operation_id,
        )
        if row is None or row["voided_at"] is not None:
            raise ValueError("Расход не найден или уже отменён.")
        if row["version"] != version:
            raise ValueError("Расход уже изменился. Обновите журнал.")
        if cancel:
            row = await conn.fetchrow(
                "UPDATE finance_operations SET voided_at=NOW(),version=version+1 WHERE id=$1 RETURNING *",
                operation_id,
            )
        else:
            row = await conn.fetchrow(
                """
                UPDATE finance_operations SET amount_kopecks=$2,category=$3,description=$4,
                    occurred_at=$5,version=version+1 WHERE id=$1 RETURNING *
            """,
                operation_id,
                *values,
            )
        await _revision(
            conn, operation_id, "canceled" if cancel else "updated", manager_id
        )
        return dict(row)
