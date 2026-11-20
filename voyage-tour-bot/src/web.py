"""Mini App и API. В боте запускается на том же event loop и пуле PostgreSQL."""

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import asyncpg
from aiohttp import web

from src.auth import AuthError, validate_init_data
from src.config import get_bot_token, get_database_url
from src.db import apply_schema, seed_products
from src.services import finance, payments
from src.services import inventory, checkout, orders
from src.services.refunds import process_refunds
from telegram import User
from src.services.money import to_kopecks
from src.services.orders import STATUS_LABELS

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "miniapp"
POOL = web.AppKey("pool", asyncpg.Pool)
TOKEN = web.AppKey("bot_token", str)
MAX_AGE = web.AppKey("auth_max_age", int)
ACTOR = web.RequestKey("actor", int)


def _default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def response(value, status=200):
    return web.json_response(
        value,
        status=status,
        dumps=lambda v: json.dumps(v, default=_default, ensure_ascii=False),
    )


@web.middleware
async def access(request, handler):
    try:
        if request.path.startswith("/api/"):
            raw = request.headers.get("Authorization", "")
            if not raw.startswith("tma "):
                raise AuthError("Откройте кабинет из Telegram-бота.")
            request[ACTOR] = validate_init_data(
                raw[4:], request.app[TOKEN], max_age=request.app[MAX_AGE]
            )
            if not request.path.startswith("/api/shop/"):
                payments.require_manager(request[ACTOR])
        result = await handler(request)
    except AuthError as exc:
        result = response({"error": str(exc)}, 401)
    except PermissionError as exc:
        result = response({"error": str(exc)}, 403)
    except (ValueError, json.JSONDecodeError) as exc:
        result = response({"error": str(exc)}, 400)
    except web.HTTPException as exc:
        result = response({"error": "Запрос не поддерживается."}, exc.status)
    except (asyncpg.PostgresError, OSError, asyncio.TimeoutError):
        logger.warning("База временно недоступна")
        result = response(
            {"error": "Сервер временно недоступен. Повторите запрос."}, 503
        )
    except Exception:
        logger.error("Не удалось обработать финансовый запрос")
        result = response(
            {"error": "Не удалось выполнить операцию. Обновите данные и повторите."},
            500,
        )
    result.headers["Cache-Control"] = "no-store"
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["Referrer-Policy"] = "no-referrer"
    result.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'"
    )
    return result


async def _body(request, allowed):
    if request.content_type != "application/json":
        raise ValueError("Ожидается JSON.")
    data = await request.json()
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError("Недопустимые поля запроса.")
    return data


def _page(request):
    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError
        return limit, offset
    except ValueError:
        raise ValueError("Некорректные параметры страницы.") from None


async def get_summary(request):
    return response(
        await finance.summary(
            request.app[POOL],
            request[ACTOR],
            request.query.get("start"),
            request.query.get("end"),
        )
    )


async def get_operations(request):
    limit, offset = _page(request)
    return response(
        await finance.operations(
            request.app[POOL],
            request[ACTOR],
            request.query.get("start"),
            request.query.get("end"),
            limit=limit,
            offset=offset,
        )
    )


async def get_orders(request):
    limit, offset = _page(request)
    status = request.query.get("status")
    if status and status not in STATUS_LABELS:
        raise ValueError("Неизвестный статус заказа.")
    async with request.app[POOL].acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id,public_number,status,recipient_name,total_amount,created_at FROM orders
            WHERE ($1::text IS NULL OR status=$1) ORDER BY created_at DESC,id DESC LIMIT $2 OFFSET $3
        """,
            status or None,
            limit,
            offset,
        )
    return response(
        [
            {
                **{k: v for k, v in dict(row).items() if k != "total_amount"},
                "amount_kopecks": to_kopecks(row["total_amount"]),
            }
            for row in rows
        ]
    )


async def get_order(request):
    async with (
        request.app[POOL].acquire() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        row = await conn.fetchrow(
            "SELECT * FROM orders WHERE public_number=$1", request.match_info["number"]
        )
        if row is None:
            return response({"error": "Заказ не найден."}, 404)
        result = dict(row)
        result["amount_kopecks"] = to_kopecks(result.pop("total_amount"))
        items = await conn.fetch(
            "SELECT * FROM order_items WHERE order_id=$1 ORDER BY id", row["id"]
        )
        result["items"] = [
            {
                "title": i["title_snapshot"],
                "quantity": i["quantity"],
                "price_kopecks": to_kopecks(i["price_snapshot"]),
                "cost_kopecks": to_kopecks(i["cost_snapshot"])
                if i["cost_snapshot"] is not None
                else None,
                "cost_estimated": i["cost_estimated"],
            }
            for i in items
        ]
        result["payments"] = [
            dict(p)
            for p in await conn.fetch(
                "SELECT * FROM payments WHERE order_id=$1 ORDER BY id DESC", row["id"]
            )
        ]
        reservation = await conn.fetchrow(
            "SELECT * FROM reservations WHERE order_id=$1", row["id"]
        )
        result["reservation"] = dict(reservation) if reservation else None
        result["server_time"] = await inventory.server_now(conn)
        result["refund_jobs"] = [
            dict(j)
            for j in await conn.fetch(
                "SELECT j.* FROM refund_jobs j JOIN payments p ON p.id=j.payment_id WHERE p.order_id=$1",
                row["id"],
            )
        ]
    return response(result)


async def post_expense(request):
    data = await _body(
        request, ("amount_kopecks", "category", "description", "date", "request_id")
    )
    return response(
        await finance.create_expense(request.app[POOL], request[ACTOR], data), 201
    )


async def update_expense(request):
    cancel = request.match_info.get("action") == "cancel"
    data = await _body(
        request,
        ("version",)
        if cancel
        else ("amount_kopecks", "category", "description", "date", "version"),
    )
    return response(
        await finance.change_expense(
            request.app[POOL],
            request[ACTOR],
            int(request.match_info["id"]),
            data,
            cancel=cancel,
        )
    )


async def refund_start(request):
    await _body(request, ())
    result = await payments.start_refund(
        request.app[POOL], request[ACTOR], request.match_info["number"]
    )
    return response(asdict(result))


async def refund_finish(request):
    await _body(request, ())
    outcome = request.match_info["outcome"]
    if outcome not in ("succeeded", "failed"):
        raise ValueError("Неизвестный результат возврата.")
    result = await payments.finish_refund(
        request.app[POOL],
        request[ACTOR],
        int(request.match_info["id"]),
        succeeded=outcome == "succeeded",
    )
    return response(asdict(result))


async def index(request):
    return web.Response(
        text=(STATIC / "index.html").read_text(encoding="utf-8"),
        content_type="text/html",
    )


async def stock_dashboard(request):
    limit, offset = _page(request)
    async with request.app[POOL].acquire() as conn:
        reservations = await conn.fetch(
            """
            SELECT r.*,o.public_number,o.status AS order_status,
                (SELECT json_agg(json_build_object('product_id',i.product_id,'quantity',i.quantity))
                 FROM reservation_items i WHERE i.reservation_id=r.id) AS items
            FROM reservations r JOIN orders o ON o.id=r.order_id WHERE r.status='active'
            ORDER BY r.expires_at LIMIT $1 OFFSET $2
        """,
            limit,
            offset,
        )
        movements = await conn.fetch(
            """
            SELECT m.*,p.title,o.public_number FROM inventory_movements m
            JOIN products p ON p.id=m.product_id LEFT JOIN orders o ON o.id=m.order_id
            ORDER BY m.id DESC LIMIT $1 OFFSET $2
        """,
            limit,
            offset,
        )
        jobs = await conn.fetch(
            """
            SELECT j.*,o.public_number,o.status AS order_status FROM refund_jobs j
            JOIN payments p ON p.id=j.payment_id JOIN orders o ON o.id=p.order_id
            ORDER BY j.id DESC LIMIT $1 OFFSET $2
        """,
            limit,
            offset,
        )
        now = await inventory.server_now(conn)
    return response(
        {
            "balances": await inventory.balances(request.app[POOL]),
            "reservations": [
                {**dict(r), "items": json.loads(r["items"])} for r in reservations
            ],
            "movements": [dict(m) for m in movements],
            "refund_jobs": [dict(j) for j in jobs],
            "server_time": now,
            "faults_enabled": os.getenv("DEMO_FAULTS_ENABLED", "false").lower()
            == "true",
        }
    )


async def stock_supply(request):
    data = await _body(request, ("product_id", "quantity", "request_id", "description"))
    if type(data.get("product_id")) is not int:
        raise ValueError("Укажите ID товара.")
    return response(
        await inventory.supply(
            request.app[POOL],
            request[ACTOR],
            data["product_id"],
            data.get("quantity"),
            data.get("request_id"),
            data.get("description"),
        )
    )


async def retry_refund(request):
    await _body(request, ())
    async with request.app[POOL].acquire() as conn:
        result = await conn.execute(
            "UPDATE refund_jobs SET next_attempt_at=NOW() WHERE id=$1 AND status='pending'",
            int(request.match_info["id"]),
        )
    if result == "UPDATE 0":
        raise ValueError("Нет ожидающего задания. Обновите данные.")
    # Обработка продолжится фоновым процессом, ключ операции не меняется.
    return response({"status": "pending"})


async def refund_fault(request):
    if os.getenv("DEMO_FAULTS_ENABLED", "false").lower() != "true":
        raise ValueError("Имитация сбоев отключена в конфигурации.")
    data = await _body(request, ("failures", "lost_responses"))
    failures, lost = data.get("failures", 0), data.get("lost_responses", 0)
    if any(type(v) is not int or not 0 <= v <= 5 for v in (failures, lost)):
        raise ValueError("Количество учебных сбоев: от 0 до 5.")
    async with request.app[POOL].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO simulator_faults(payment_id,failures_left,lost_responses_left) VALUES ($1,$2,$3)
            ON CONFLICT (payment_id) DO UPDATE SET failures_left=$2,lost_responses_left=$3
        """,
            int(request.match_info["id"]),
            failures,
            lost,
        )
    return response({"configured": True})


def _customer_order(order):
    return {
        "id": order.id,
        "public_number": order.public_number,
        "status": order.status,
        "amount_kopecks": to_kopecks(order.total_amount),
        "expires_at": order.expires_at,
        "reservation_status": order.reservation_status,
        "items": [
            {
                "title": i.title,
                "quantity": i.quantity,
                "price_kopecks": to_kopecks(i.price),
            }
            for i in order.items
        ],
    }


async def shop_catalog(request):
    return response(await inventory.balances(request.app[POOL]))


async def shop_cart(request):
    data = await _body(request, ("items",))
    _, _ = checkout._payload("Покупатель", "office", data.get("items"))
    items = {int(k): v for k, v in data["items"].items()}
    async with request.app[POOL].acquire() as conn, conn.transaction():
        uid = await orders._user_id(conn, User(request[ACTOR], "Покупатель", False))
        valid = await conn.fetchval(
            "SELECT count(*) FROM products WHERE id=ANY($1::bigint[]) AND is_active",
            sorted(items),
        )
        if valid != len(items):
            raise ValueError("Товар не найден.")
        await conn.execute("DELETE FROM cart_items WHERE user_id=$1", uid)
        for pid, qty in sorted(items.items()):
            await conn.execute(
                "INSERT INTO cart_items(user_id,product_id,quantity) VALUES ($1,$2,$3)",
                uid,
                pid,
                qty,
            )
    return response({"items": items})


async def shop_checkout(request):
    data = await _body(
        request, ("recipient_name", "pickup_method", "request_id", "items")
    )
    if not data.get("request_id") or "items" not in data:
        raise ValueError("Нужны постоянный request_id и состав корзины.")
    result = await checkout.create_order(
        request.app[POOL],
        User(request[ACTOR], "Покупатель", False),
        data.get("recipient_name"),
        data.get("pickup_method"),
        request_id=data["request_id"],
        expected_cart=data["items"],
    )
    return response(_customer_order(result), 201)


async def shop_orders(request):
    return response(
        [
            _customer_order(o)
            for o in await orders.list_orders(request.app[POOL], request[ACTOR])
        ]
    )


async def shop_payment_start(request):
    await _body(request, ())
    return response(
        asdict(
            await payments.start_payment(
                request.app[POOL], request[ACTOR], request.match_info["number"]
            )
        )
    )


async def shop_payment_result(request):
    # Учебный исход подтверждается только владельцем сохранённого платежа.
    await _body(request, ())
    outcome = request.match_info["outcome"]
    if outcome not in ("succeeded", "canceled"):
        raise ValueError("Неизвестный исход имитации.")
    function = (
        payments.simulate_success
        if outcome == "succeeded"
        else payments.simulate_cancel
    )
    result = await function(
        request.app[POOL], request[ACTOR], int(request.match_info["id"])
    )
    from src.services.demo_faults import checkpoint

    async with request.app[POOL].acquire() as conn:
        await checkpoint(conn, "after_payment_commit")
    return response(asdict(result))


async def shop_cancel(request):
    await _body(request, ())
    return response(
        {
            "status": await inventory.cancel_order(
                request.app[POOL], request[ACTOR], request.match_info["number"]
            )
        }
    )


async def asset(request):
    name = request.match_info["name"]
    if name not in ("app.js", "style.css"):
        raise web.HTTPNotFound()
    return web.Response(
        text=(STATIC / name).read_text(encoding="utf-8"),
        content_type="text/javascript" if name.endswith(".js") else "text/css",
    )


def create_app(pool, token, *, max_age=3600):
    app = web.Application(middlewares=[access], client_max_size=16384)
    app[POOL], app[TOKEN], app[MAX_AGE] = pool, token, max_age
    app.add_routes(
        [
            web.get("/", index),
            web.get("/static/{name}", asset),
            web.get("/api/summary", get_summary),
            web.get("/api/operations", get_operations),
            web.get("/api/orders", get_orders),
            web.get("/api/orders/{number}", get_order),
            web.post("/api/expenses", post_expense),
            web.put("/api/expenses/{id}", update_expense),
            web.post("/api/expenses/{id}/{action:cancel}", update_expense),
            web.post("/api/orders/{number}/refund", refund_start),
            web.post("/api/payments/{id}/refund/{outcome}", refund_finish),
            web.get("/api/stock", stock_dashboard),
            web.post("/api/stock/supplies", stock_supply),
            web.post("/api/refund-jobs/{id}/retry", retry_refund),
            web.post("/api/payments/{id}/fault", refund_fault),
            web.get("/api/shop/catalog", shop_catalog),
            web.put("/api/shop/cart", shop_cart),
            web.get("/api/shop/orders", shop_orders),
            web.post("/api/shop/checkout", shop_checkout),
            web.post("/api/shop/orders/{number}/payment", shop_payment_start),
            web.post("/api/shop/orders/{number}/cancel", shop_cancel),
            web.post("/api/shop/payments/{id}/{outcome}", shop_payment_result),
        ]
    )
    return app


async def start_web(pool, token):
    app = create_app(pool, token, max_age=int(os.getenv("INIT_DATA_MAX_AGE", "3600")))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(
            runner,
            os.getenv("WEB_HOST", "127.0.0.1"),
            int(os.getenv("WEB_PORT", "8080")),
        ).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


async def standalone():
    token = get_bot_token()
    dsn = get_database_url()
    if not dsn:
        raise RuntimeError("Укажите DATABASE_URL в .env.")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    runner = None
    tasks = []
    try:
        await apply_schema(pool)
        await seed_products(pool)
        await inventory.expire_reservations(pool)
        await process_refunds(pool)
        from src.services.refunds import refund_worker

        tasks = [
            asyncio.create_task(inventory.inventory_worker(pool)),
            asyncio.create_task(refund_worker(pool)),
        ]
        runner = await start_web(pool, token)
        print("Mini App: http://127.0.0.1:" + os.getenv("WEB_PORT", "8080"))
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner:
            await runner.cleanup()
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(standalone())
    except KeyboardInterrupt:
        pass
