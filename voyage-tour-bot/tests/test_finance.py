import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

from src.auth import AuthError, validate_init_data
from src.db import apply_schema
from src.services import finance, orders, payments
from src.services import inventory
from src.handlers import checkout as checkout_handler
from src.handlers import orders as orders_handler
from src.handlers import payments as payment_handler
from src.services.checkout import prepare_checkout, complete_checkout
from src.services.money import format_kopecks, to_kopecks
from src.services.notifications import deliver_notifications
from src.web import create_app
from tests.support import MANAGER, TOKEN, TestDatabase, auth, cart


class AuthTests(unittest.TestCase):
    def test_signature_age_and_duplicates(self):
        valid = auth(MANAGER)[4:]
        self.assertEqual(validate_init_data(valid, TOKEN), MANAGER)
        for raw in (
            valid + "&user=x",
            auth(MANAGER, timestamp=int(time.time()) - 4000)[4:],
            auth(MANAGER, timestamp=int(time.time()) + 100)[4:],
            valid.replace("hash=", "hash=f"),
            "",
        ):
            with self.assertRaises(AuthError):
                validate_init_data(raw, TOKEN)

    def test_money(self):
        self.assertEqual(to_kopecks("1200.99"), 120099)
        self.assertEqual(format_kopecks(120099), "1 200,99 ₽")
        for value in ("1.001", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                to_kopecks(value)


class FinanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"MANAGER_TELEGRAM_ID": str(MANAGER)})
        self.env.start()
        self.db = TestDatabase()
        self.pool = await self.db.open()
        await inventory.supply(self.pool, MANAGER, 1, 20, str(uuid4()), "Учебные места")
        self.client = TestClient(TestServer(create_app(self.pool, TOKEN)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.db.close()
        self.env.stop()

    async def order(self, user=101):
        u = await cart(self.pool, user, {1: 1})
        return await orders.create_order(self.pool, u, "Учебный клиент", "office")

    async def test_checkout_button_recovers_after_lost_response(self):
        """Потеря ответа после commit: та же кнопка восстанавливает единственный заказ."""
        user = await cart(self.pool, 101, {1: 1})
        key = await prepare_checkout(self.pool, user, "Учебный клиент", "office")
        query = AsyncMock()
        query.data = f"checkout:ok:{key}"
        update = SimpleNamespace(effective_user=user, callback_query=query)
        context = SimpleNamespace(user_data={})

        async def lose_response(pool, buyer, request_key):
            await complete_checkout(pool, buyer, request_key)
            raise TimeoutError("test: lost confirmation")

        with patch.object(checkout_handler, "get_pool", return_value=self.pool):
            with (
                patch.object(
                    checkout_handler, "complete_checkout", side_effect=lose_response
                ),
                self.assertLogs(checkout_handler.logger, level="ERROR"),
            ):
                await checkout_handler.confirm_order(update, context)
            retry = query.edit_message_text.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertEqual(
                (retry.text, retry.callback_data), ("Повторить оформление", query.data)
            )
            await checkout_handler.confirm_order(update, context)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM orders"), 1)
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT count(*) FROM reservations WHERE status='active'"
            ),
            1,
        )

    async def test_canceled_payment_buttons_after_reopening_order(self):
        """Отмена платежа, повторное открытие заказа, освобождение мест и возврат в каталог."""
        order = await self.order()
        payment = await payments.start_payment(self.pool, 101, order.public_number)
        query = AsyncMock()
        query.data = f"pay:cancel:{payment.id}"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=101),
            callback_query=query,
            message=AsyncMock(),
        )
        release = f"pay:release:{order.public_number}"

        def buttons(mock):
            markup = mock.await_args.kwargs["reply_markup"]
            return [
                button.callback_data for row in markup.inline_keyboard for button in row
            ]

        with (
            patch.object(payment_handler, "get_pool", return_value=self.pool),
            patch.object(orders_handler, "get_pool", return_value=self.pool),
        ):
            await payment_handler.on_payment_callback(update, None)
            self.assertEqual(buttons(query.edit_message_text), [release])
            await orders_handler.show_orders(update, None)
            self.assertEqual(buttons(update.message.reply_text), [release])
            query.data = release
            await payment_handler.on_payment_callback(update, None)
            self.assertEqual(buttons(query.edit_message_text), ["tour:list"])
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT reserved FROM stock_balances WHERE product_id=1"
            ),
            0,
        )

    async def test_price_and_cost_snapshots_keep_kopecks(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE products SET price=1200.99,cost_price=700.49 WHERE id=1"
            )
        order = await self.order()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE products SET price=9999,cost_price=8888 WHERE id=1"
            )
        payment = await payments.start_payment(self.pool, 101, order.public_number)
        self.assertEqual(payment.amount_kopecks, 120099)
        await payments.simulate_success(self.pool, 101, payment.id)
        summary = await finance.summary(self.pool, MANAGER)
        self.assertEqual(
            (summary["receipts"], summary["cost"], summary["learning_result"]),
            (120099, 70049, 50050),
        )
        await payments.start_refund(self.pool, MANAGER, order.public_number)
        await payments.finish_refund(self.pool, MANAGER, payment.id, succeeded=True)
        self.assertEqual(
            (await finance.summary(self.pool, MANAGER))["learning_result"], 0
        )

    async def test_full_lifecycle_and_persistence(self):
        order = await self.order()
        p = await payments.start_payment(self.pool, 101, order.public_number)
        self.assertEqual((await finance.summary(self.pool, MANAGER))["receipts"], 0)
        results = await asyncio.gather(
            *(payments.simulate_success(self.pool, 101, p.id) for _ in range(5))
        )
        self.assertEqual(sum(not r.already_receipt for r in results), 1)
        s = await finance.summary(self.pool, MANAGER)
        self.assertEqual((s["cash_flow"], s["learning_result"]), (8900000, 3400000))
        bot = AsyncMock()
        await deliver_notifications(self.pool, bot)
        await deliver_notifications(self.pool, bot)
        self.assertEqual(bot.send_message.await_count, 1)
        await finance.create_expense(
            self.pool,
            MANAGER,
            {
                "amount_kopecks": 10000,
                "category": "delivery",
                "description": "Трансфер",
                "date": finance.datetime.now(finance.MOSCOW).date().isoformat(),
                "request_id": str(uuid4()),
            },
        )
        s = await finance.summary(self.pool, MANAGER)
        self.assertEqual((s["cash_flow"], s["learning_result"]), (8890000, 3390000))
        await payments.start_refund(self.pool, MANAGER, order.public_number)
        self.assertEqual(await finance.summary(self.pool, MANAGER), s)
        await payments.finish_refund(self.pool, MANAGER, p.id, succeeded=False)
        self.assertEqual(await finance.summary(self.pool, MANAGER), s)
        await payments.start_refund(self.pool, MANAGER, order.public_number)
        await payments.finish_refund(self.pool, MANAGER, p.id, succeeded=True)
        for action in (
            payments.start_refund(self.pool, MANAGER, order.public_number),
            payments.finish_refund(self.pool, MANAGER, p.id, succeeded=True),
        ):
            with self.assertRaises(payments.PaymentError):
                await action
        self.assertTrue(
            (await payments.simulate_success(self.pool, 101, p.id)).already_receipt
        )
        final = await finance.summary(self.pool, MANAGER)
        self.assertEqual(
            (final["cash_flow"], final["learning_result"]), (-10000, -10000)
        )
        await self.client.close()
        await self.pool.expire_connections()
        await apply_schema(self.pool)
        self.assertEqual(await finance.summary(self.pool, MANAGER), final)
        self.assertEqual(len(await finance.operations(self.pool, MANAGER)), 3)

    async def test_cancel_ownership_amount_currency_and_rollback(self):
        o = await self.order()
        p = await payments.start_payment(self.pool, 101, o.public_number)
        for kwargs in ({"amount_kopecks": 1}, {"currency": "USD"}):
            with self.assertRaises(payments.PaymentError):
                await payments.simulate_success(self.pool, 101, p.id, **kwargs)
        with self.assertRaises(payments.PaymentError):
            await payments.start_payment(self.pool, 102, o.public_number)
        with self.assertRaises(payments.PaymentError):
            await payments.simulate_success(self.pool, 102, p.id)
        self.assertEqual(await orders.list_orders(self.pool, 102), [])
        await payments.simulate_cancel(self.pool, 101, p.id)
        with self.assertRaises(payments.PaymentError):
            await payments.simulate_success(self.pool, 101, p.id)
        self.assertEqual((await finance.summary(self.pool, MANAGER))["receipts"], 0)
        with self.assertRaises(payments.PaymentError):
            await payments.start_payment(self.pool, 101, o.public_number)

    async def test_expense_validation_idempotence_audit(self):
        data = {
            "amount_kopecks": 10000,
            "category": "delivery",
            "description": "Трансфер",
            "date": finance.datetime.now(finance.MOSCOW).date().isoformat(),
            "request_id": str(uuid4()),
        }
        for amount in (0, -100, 1.5, True):
            with self.assertRaises(ValueError):
                await finance.create_expense(
                    self.pool, MANAGER, {**data, "amount_kopecks": amount}
                )
        with self.assertRaises(PermissionError):
            await finance.create_expense(self.pool, 101, data)
        a, b = await asyncio.gather(
            *(finance.create_expense(self.pool, MANAGER, data) for _ in range(2))
        )
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(ValueError):
            await finance.create_expense(
                self.pool, MANAGER, {**data, "amount_kopecks": 20000}
            )
        changed = await finance.change_expense(
            self.pool, MANAGER, a["id"], {**data, "amount_kopecks": 20000, "version": 1}
        )
        with self.assertRaises(ValueError):
            await finance.change_expense(
                self.pool, MANAGER, a["id"], {"version": 1}, cancel=True
            )
        await finance.change_expense(
            self.pool, MANAGER, a["id"], {"version": changed["version"]}, cancel=True
        )
        self.assertEqual((await finance.summary(self.pool, MANAGER))["expenses"], 0)
        self.assertEqual(
            len((await finance.operations(self.pool, MANAGER))[0]["revisions"]), 3
        )

    async def test_api_permissions_and_periods(self):
        paths = (
            "/api/summary",
            "/api/orders",
            "/api/orders/VT-000001",
            "/api/operations",
        )
        for path in paths:
            for header, status in (
                ({}, 401),
                ({"Authorization": auth(101)}, 403),
                ({"Authorization": auth(MANAGER, timestamp=1)}, 401),
            ):
                r = await self.client.get(path, headers=header)
                self.assertEqual(r.status, status)
        r = await self.client.get(
            "/api/summary?start=2026-12-01&end=2026-01-01",
            headers={"Authorization": auth(MANAGER)},
        )
        self.assertEqual(r.status, 400)
        r = await self.client.post(
            "/api/expenses",
            json={"amount_kopecks": 0},
            headers={"Authorization": auth(101)},
        )
        self.assertEqual(r.status, 403)
        r = await self.client.post(
            "/api/orders/VT-000001/refund",
            json={"amount_kopecks": 1},
            headers={"Authorization": auth(MANAGER)},
        )
        self.assertEqual(r.status, 400)
        o = await self.order()
        p = await payments.start_payment(self.pool, 101, o.public_number)
        await payments.simulate_success(self.pool, 101, p.id)
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE finance_operations SET occurred_at='2026-01-31 23:00:00+03' WHERE type='receipt'"
            )
        await payments.start_refund(self.pool, MANAGER, o.public_number)
        await payments.finish_refund(self.pool, MANAGER, p.id, succeeded=True)
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE finance_operations SET occurred_at='2026-02-01 00:00:00+03' WHERE type='refund'"
            )
        jan = await finance.summary(self.pool, MANAGER, "2026-01-01", "2026-01-31")
        feb = await finance.summary(self.pool, MANAGER, "2026-02-01", "2026-02-28")
        self.assertEqual(
            (jan["learning_result"], feb["learning_result"]), (3400000, -3400000)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
