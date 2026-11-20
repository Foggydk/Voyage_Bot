import asyncio
import os
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from src.services import inventory, payments, finance
from src.services.refunds import process_refunds
from src.web import create_app
from tests.support import MANAGER, TOKEN, TestDatabase, auth, cart


class StockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "MANAGER_TELEGRAM_ID": str(MANAGER),
                "RESERVATION_SECONDS": os.getenv(
                    "STOCK_TEST_RESERVATION_SECONDS", "120"
                ),
                "DEMO_FAULTS_ENABLED": "true",
            },
        )
        self.env.start()
        self.db = TestDatabase()
        self.pool = await self.db.open()
        self.client = TestClient(TestServer(create_app(self.pool, TOKEN)))
        await self.client.start_server()
        self.processes = []
        self.evidence = []
        async with self.pool.acquire() as c:
            await c.execute("UPDATE products SET price=1200,cost_price=700")

    async def asyncTearDown(self):
        for p in self.processes:
            if p.returncode is None:
                p.terminate()
            await p.wait()
        await self.client.close()
        await self.db.close()
        self.env.stop()

    async def record(self, scenario, expected, actual):
        orders = await self.pool.fetch("""
            SELECT o.public_number,p.id AS payment_id FROM orders o
            LEFT JOIN payments p ON p.order_id=o.id ORDER BY o.id,p.id
        """)
        self.evidence.append(
            {
                "scenario": scenario,
                "expected": expected,
                "actual": actual,
                "orders": [dict(row) for row in orders],
            }
        )

    async def call(self, method, path, user=MANAGER, data=None, client=None):
        client = client or self.client
        async with client.request(
            method, "/api/" + path, json=data, headers={"Authorization": auth(user)}
        ) as r:
            return r.status, await r.json()

    async def supply(self, qty, product=1):
        status, d = await self.call(
            "POST",
            "stock/supplies",
            data={
                "product_id": product,
                "quantity": qty,
                "request_id": str(uuid4()),
                "description": "Тестовая квота",
            },
        )
        self.assertEqual(status, 200, d)
        return d

    async def checkout(self, user=101, items=None, key=None, client=None):
        items = items or {1: 1}
        key = key or str(uuid4())
        return await self.call(
            "POST",
            "shop/checkout",
            user,
            {
                "recipient_name": "Учебный клиент",
                "pickup_method": "office",
                "request_id": key,
                "items": items,
            },
            client,
        )

    async def new_order(self, user=101, items=None):
        await cart(self.pool, user, items or {1: 1})
        status, o = await self.checkout(user, items)
        self.assertEqual(status, 201, o)
        status, p = await self.call(
            "POST", f"shop/orders/{o['public_number']}/payment", user, {}
        )
        self.assertEqual(status, 200, p)
        return o, p

    async def balance(self, product=1):
        async with self.pool.acquire() as c:
            r = await c.fetchrow(
                "SELECT on_hand,reserved,on_hand-reserved AS available FROM stock_balances WHERE product_id=$1",
                product,
            )
        return tuple(r.values())

    async def assert_invariants(self):
        async with self.pool.acquire() as c:
            broken = await c.fetchval("""
                SELECT count(*) FROM stock_balances b WHERE on_hand<0 OR reserved<0 OR reserved>on_hand OR
                reserved != COALESCE((SELECT SUM(i.quantity) FROM reservation_items i JOIN reservations r ON r.id=i.reservation_id
                                     WHERE i.product_id=b.product_id AND r.status='active'),0)
            """)
            self.assertEqual(broken, 0)
            mismatch = await c.fetchval("""
                SELECT count(*) FROM stock_balances b WHERE on_hand != COALESCE((SELECT SUM(delta_on_hand) FROM inventory_movements WHERE product_id=b.product_id),0)
                    OR reserved != COALESCE((SELECT SUM(delta_reserved) FROM inventory_movements WHERE product_id=b.product_id),0)
            """)
            self.assertEqual(mismatch, 0)

    async def expire(self, order):
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE reservations SET expires_at=clock_timestamp()-INTERVAL '1 second' WHERE order_id=$1",
                order["id"],
            )
        await inventory.expire_reservations(self.pool)

    async def fresh(self):
        async with self.pool.acquire() as c:
            self.assertTrue(
                (await c.fetchval("SELECT current_database()")).startswith(
                    "voyage_test_"
                )
            )
            await c.execute(
                "TRUNCATE users,cart_items,orders,order_items,payments,finance_operations,expense_revisions,notification_outbox,reservations,reservation_items,inventory_movements,checkout_requests,refund_jobs,simulator_refunds,simulator_faults RESTART IDENTITY CASCADE"
            )
            await c.execute("UPDATE stock_balances SET on_hand=0,reserved=0")

    async def test_01_manual_chain(self):
        """Поступление→резерв→продажа→резерв→истечение→возврат: шесть точных троек."""
        await self.supply(10)
        actual = [await self.balance()]
        a, p = await self.new_order(101, {1: 3})
        actual.append(await self.balance())
        await self.call("POST", f"shop/payments/{p['id']}/succeeded", 101, {})
        actual.append(await self.balance())
        b, _ = await self.new_order(102, {1: 2})
        actual.append(await self.balance())
        await self.expire(b)
        actual.append(await self.balance())
        await self.call("POST", f"orders/{a['public_number']}/refund", data={})
        self.assertEqual(await self.balance(), (7, 0, 7))
        await self.call("POST", f"payments/{p['id']}/refund/succeeded", data={})
        actual.append(await self.balance())
        expected = [
            (10, 0, 10),
            (10, 3, 7),
            (7, 0, 7),
            (7, 2, 5),
            (7, 0, 7),
            (10, 0, 10),
        ]
        self.assertEqual(actual, expected)
        await self.assert_invariants()
        await self.record("Ручная цепочка", expected, actual)

    async def test_02_03_concurrent_buyers(self):
        """HTTP-барьер: 1/20 и 10/50; минимум пять свежих запусков каждого."""
        matrix = [(1, 20), (10, 50)]
        if os.getenv("STOCK_TEST_STOCK"):
            matrix = [
                (
                    int(os.environ["STOCK_TEST_STOCK"]),
                    int(os.environ["STOCK_TEST_BUYERS"]),
                )
            ]
        repeats = max(5, int(os.getenv("STOCK_TEST_REPEATS", "5")))
        for stock, buyers in matrix:
            for iteration in range(repeats):
                await self.fresh()
                await self.supply(stock)
                for i in range(buyers):
                    await cart(self.pool, 1000 + i, {1: 1})
                gate = asyncio.Event()

                async def buy(i):
                    await gate.wait()
                    return await self.checkout(1000 + i)

                tasks = [asyncio.create_task(buy(i)) for i in range(buyers)]
                gate.set()
                results = await asyncio.gather(*tasks)
                accepted = sum(status == 201 for status, _ in results)
                rejected = sum(
                    status == 400 and "нет в наличии" in d.get("error", "")
                    for status, d in results
                )
                self.assertEqual(
                    (accepted, rejected),
                    (min(stock, buyers), max(0, buyers - stock)),
                    results,
                )
                self.assertEqual(
                    await self.balance(),
                    (stock, min(stock, buyers), max(0, stock - buyers)),
                )
                async with self.pool.acquire() as c:
                    self.assertEqual(
                        await c.fetchval("SELECT count(*) FROM orders"), accepted
                    )
                await self.assert_invariants()
                await self.record(
                    f"{stock} мест / {buyers} покупателей, повтор {iteration + 1}",
                    f"{min(stock, buyers)} успехов, {max(0, buyers - stock)} штатных отказов",
                    f"{accepted} успехов, {rejected} штатных отказов",
                )

    async def test_04_multi_item_rollback(self):
        """Нехватка второй позиции откатывает весь заказ, резерв и журнал."""
        await self.supply(3)
        await cart(self.pool, 101, {1: 1, 2: 1})
        status, d = await self.checkout(101, {1: 1, 2: 1})
        self.assertEqual(status, 400, d)
        self.assertEqual(await self.balance(), (3, 0, 3))
        async with self.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM orders"), 0)
            self.assertEqual(await c.fetchval("SELECT count(*) FROM reservations"), 0)
            self.assertEqual(await c.fetchval("SELECT count(*) FROM cart_items"), 2)
        await self.assert_invariants()
        await self.record(
            "Многотоварный отказ",
            "А=3/0/3, заказов=0",
            f"А={await self.balance()}, заказов=0",
        )

    async def test_05_checkout_idempotency(self):
        """Один ключ и состав дают один заказ; изменение состава отклоняется."""
        await self.supply(10)
        await cart(self.pool, 101, {1: 1})
        key = str(uuid4())
        gate = asyncio.Event()

        async def submit():
            await gate.wait()
            return await self.checkout(101, key=key)

        tasks = [asyncio.create_task(submit()) for _ in range(10)]
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertTrue(all(s == 201 for s, _ in results), results)
        self.assertEqual(len({d["id"] for _, d in results}), 1)
        status, d = await self.checkout(101, {1: 2}, key)
        self.assertEqual(status, 400, d)
        self.assertEqual(await self.balance(), (10, 1, 9))
        await self.assert_invariants()
        await self.record(
            "10 повторов оформления",
            "1 заказ; изменённый состав отклонён",
            f"{len({d['id'] for _, d in results})} заказ; HTTP {status}",
        )

    async def test_06_payment_repeats_and_stale_cancel(self):
        """Пять подтверждений создают одну продажу и 1200 ₽; canceled не стирает успех."""
        await self.supply(1)
        o, p = await self.new_order()
        gate = asyncio.Event()

        async def pay():
            await gate.wait()
            return await self.call(
                "POST", f"shop/payments/{p['id']}/succeeded", 101, {}
            )

        tasks = [asyncio.create_task(pay()) for _ in range(5)]
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertTrue(all(s == 200 for s, _ in results), results)
        self.assertEqual(await self.balance(), (0, 0, 0))
        status, _ = await self.call(
            "POST", f"shop/payments/{p['id']}/canceled", 101, {}
        )
        self.assertEqual(status, 400)
        s = await finance.summary(self.pool, MANAGER)
        self.assertEqual((s["receipts"], s["cost"]), (120000, 70000))
        async with self.pool.acquire() as c:
            self.assertEqual(
                await c.fetchval(
                    "SELECT count(*) FROM inventory_movements WHERE type='sale'"
                ),
                1,
            )
        await self.assert_invariants()
        await self.record(
            "5 подтверждений и canceled",
            "1 продажа, 120000 коп.",
            f"1 продажа, {s['receipts']} коп.",
        )

    async def test_07_cancel_twice_and_expiry(self):
        """Двойная отмена и повторная обработка срока освобождают каждый резерв один раз."""
        await self.supply(2)
        a, _ = await self.new_order(101)
        b, _ = await self.new_order(102)
        for _ in range(2):
            self.assertEqual(
                (
                    await self.call(
                        "POST", f"shop/orders/{a['public_number']}/cancel", 101, {}
                    )
                )[0],
                200,
            )
        await self.expire(b)
        await inventory.expire_reservations(self.pool)
        self.assertEqual(await self.balance(), (2, 0, 2))
        self.assertEqual((await finance.summary(self.pool, MANAGER))["receipts"], 0)
        async with self.pool.acquire() as c:
            self.assertEqual(
                await c.fetchval(
                    "SELECT count(*) FROM inventory_movements WHERE type='release'"
                ),
                2,
            )
        await self.assert_invariants()
        await self.record(
            "Отмена и срок",
            "2/0/2; два освобождения",
            f"{await self.balance()}; два освобождения",
        )

    async def test_08_races_and_exact_boundary(self):
        """Гонки оплаты/отмены и оплаты/истечения; строго до, ровно и после срока."""
        for iteration in range(5):
            await self.fresh()
            await self.supply(1)
            o, p = await self.new_order()
            gate = asyncio.Event()

            async def pay():
                await gate.wait()
                return await self.call(
                    "POST", f"shop/payments/{p['id']}/succeeded", 101, {}
                )

            async def cancel():
                await gate.wait()
                return await self.call(
                    "POST", f"shop/orders/{o['public_number']}/cancel", 101, {}
                )

            tasks = [asyncio.create_task(pay()), asyncio.create_task(cancel())]
            gate.set()
            results = await asyncio.gather(*tasks)
            self.assertEqual(results[0][0], 200, results)
            winner = results[0][1]["order_status"]
            self.assertIn(winner, ("paid", "payment_conflict"))
            await process_refunds(self.pool, force=True)
            self.assertEqual(
                await self.balance(), (0, 0, 0) if winner == "paid" else (1, 0, 1)
            )
            await self.assert_invariants()
            await self.record(
                f"Гонка оплата/отмена {iteration + 1}",
                "paid или payment_conflict без двойных движений",
                winner,
            )
        for delta in (-1, 0, 1):
            await self.fresh()
            await self.supply(1)
            o, p = await self.new_order()
            async with self.pool.acquire() as c:
                expiry = await c.fetchval(
                    "SELECT expires_at FROM reservations WHERE order_id=$1", o["id"]
                )
            result = await payments.simulate_success(
                self.pool, 101, p["id"], now=expiry + timedelta(microseconds=delta)
            )
            self.assertEqual(
                result.order_status, "paid" if delta < 0 else "payment_conflict"
            )
            await process_refunds(self.pool, force=True)
            await self.assert_invariants()
            await self.record(
                f"Граница срока {delta} мкс",
                "paid" if delta < 0 else "payment_conflict",
                result.order_status,
            )
        for iteration in range(5):
            await self.fresh()
            await self.supply(1)
            o, p = await self.new_order()
            async with self.pool.acquire() as c:
                await c.execute(
                    "UPDATE reservations SET expires_at=clock_timestamp()-INTERVAL '1 microsecond' WHERE order_id=$1",
                    o["id"],
                )
            gate = asyncio.Event()

            async def paylate():
                await gate.wait()
                return await self.call(
                    "POST", f"shop/payments/{p['id']}/succeeded", 101, {}
                )

            async def expire():
                await gate.wait()
                return await inventory.expire_reservations(self.pool)

            tasks = [asyncio.create_task(paylate()), asyncio.create_task(expire())]
            gate.set()
            result, _ = await asyncio.gather(*tasks)
            self.assertEqual(result[1]["order_status"], "payment_conflict", result)
            await process_refunds(self.pool, force=True)
            self.assertEqual(await self.balance(), (1, 0, 1))
            await self.assert_invariants()
            await self.record(
                f"Гонка оплата/срок {iteration + 1}",
                "payment_conflict; 1/0/1",
                f"{result[1]['order_status']}; {await self.balance()}",
            )

    async def test_09_late_payment_after_other_sale(self):
        """А просрочен, Б купил последнее место: 2×1200−1200, себестоимость только Б."""
        await self.supply(1)
        a, pa = await self.new_order(101)
        await self.expire(a)
        b, pb = await self.new_order(102)
        await self.call("POST", f"shop/payments/{pb['id']}/succeeded", 102, {})
        status, d = await self.call(
            "POST", f"shop/payments/{pa['id']}/succeeded", 101, {}
        )
        self.assertEqual((status, d["order_status"]), (200, "payment_conflict"))
        before = await finance.summary(self.pool, MANAGER)
        self.assertEqual((before["receipts"], before["cost"]), (240000, 70000))
        await process_refunds(self.pool, force=True)
        for _ in range(3):
            await self.call("POST", f"shop/payments/{pa['id']}/succeeded", 101, {})
        after = await finance.summary(self.pool, MANAGER)
        self.assertEqual(
            (after["cash_flow"], after["learning_result"], after["returned_cost"]),
            (120000, 50000, 0),
        )
        self.assertEqual(await self.balance(), (0, 0, 0))
        await self.assert_invariants()
        await self.record(
            "Поздняя оплата А после продажи Б",
            "Поток 120000, результат 50000, склад 0/0/0",
            f"Поток {after['cash_flow']}, результат {after['learning_result']}, склад {await self.balance()}",
        )

    async def test_14_two_server_processes(self):
        """Два независимых HTTP-процесса и пула: последняя единица продаётся одному из 20."""
        _, base_a = await self.start_process()
        _, base_b = await self.start_process()
        for iteration in range(5):
            await self.fresh()
            await self.supply(1)
            for i in range(20):
                await cart(self.pool, 3000 + i, {1: 1})
            gate = asyncio.Event()

            async def buy(i):
                await gate.wait()
                return await self.process_post(
                    base_a if i % 2 else base_b,
                    "shop/checkout",
                    3000 + i,
                    {
                        "recipient_name": "Учебный клиент",
                        "pickup_method": "office",
                        "request_id": str(uuid4()),
                        "items": {"1": 1},
                    },
                )

            tasks = [asyncio.create_task(buy(i)) for i in range(20)]
            gate.set()
            results = await asyncio.gather(*tasks)
            actual = (
                sum(s == 201 for s, _ in results),
                sum(
                    s == 400 and "нет в наличии" in d.get("error", "")
                    for s, d in results
                ),
            )
            self.assertEqual(actual, (1, 19), results)
            await self.assert_invariants()
            await self.record(
                f"Два сервера, повтор {iteration + 1}",
                "1 успех, 19 отказов",
                f"{actual[0]} успех, {actual[1]} отказов",
            )

    async def test_15_legacy_orders_preserve_stock(self):
        """Историческая продажа не списывает новый склад; старая неоплаченная заявка получает резерв."""
        await self.supply(2)
        async with self.pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users(telegram_id) VALUES (101) RETURNING id"
            )
            oid = await conn.fetchval(
                """
                INSERT INTO orders(public_number,user_id,status,recipient_name,pickup_method,total_amount)
                VALUES ('VT-LEGACY',$1,'awaiting_payment','Учебный клиент','office',1200) RETURNING id
            """,
                uid,
            )
            await conn.execute(
                "INSERT INTO order_items(order_id,product_id,title_snapshot,price_snapshot,cost_snapshot,quantity) VALUES ($1,1,'Старый тур',1200,700,1)",
                oid,
            )
        payment = await payments.start_payment(self.pool, 101, "VT-LEGACY")
        self.assertEqual(await self.balance(), (2, 1, 1))
        await payments.simulate_success(self.pool, 101, payment.id)
        self.assertEqual(await self.balance(), (1, 0, 1))
        # Отдельная историческая уже оплаченная заявка не имеет складского события.
        async with self.pool.acquire() as conn:
            old = await conn.fetchval(
                """
                INSERT INTO orders(public_number,user_id,status,recipient_name,pickup_method,total_amount)
                VALUES ('VT-PAST',$1,'paid','Прежний клиент','office',1200) RETURNING id
            """,
                uid,
            )
            await conn.execute(
                "INSERT INTO order_items(order_id,product_id,title_snapshot,price_snapshot,cost_snapshot,quantity) VALUES ($1,1,'Исторический тур',1200,700,1)",
                old,
            )
            pid = await conn.fetchval(
                "INSERT INTO payments(order_id,amount_kopecks,currency,status,provider) VALUES ($1,120000,'RUB','succeeded','simulator') RETURNING id",
                old,
            )
            await conn.execute(
                "INSERT INTO finance_operations(type,amount_kopecks,cost_kopecks,order_id,payment_id) VALUES ('receipt',120000,70000,$1,$2)",
                old,
                pid,
            )
        from src.db import apply_schema, seed_products

        await apply_schema(self.pool)
        await seed_products(self.pool)
        self.assertEqual(await self.balance(), (1, 0, 1))
        await payments.start_refund(self.pool, MANAGER, "VT-PAST")
        await payments.finish_refund(self.pool, MANAGER, pid, succeeded=True)
        self.assertEqual(await self.balance(), (1, 0, 1))
        await self.assert_invariants()
        await self.record(
            "Прежние заказы",
            "Старая ожидающая: резерв; старый оплаченный возврат: без движения мест",
            f"Склад {await self.balance()} после миграции и возврата",
        )

    async def start_process(self, failpoint=None):
        env = {
            **os.environ,
            "TEST_DATABASE_URL": self.db.dsn,
            "TEST_MODE": "true",
            "TEST_FAILPOINT": failpoint or "",
        }
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.server",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(p)
        line = await asyncio.wait_for(p.stdout.readline(), 15)
        self.assertTrue(line.startswith(b"READY "), "Тестовый сервер не запустился")
        return p, "http://127.0.0.1:" + line.decode().split()[1]

    async def process_post(self, base, path, user, data):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                base + "/api/" + path, json=data, headers={"Authorization": auth(user)}
            ) as r:
                return r.status, await r.json()

    async def test_10_restart_expired_reservation(self):
        """Принудительный останов HTTP-сервера; после срока новый процесс освобождает резерв."""
        await self.supply(1)
        o, _ = await self.new_order()
        p, base = await self.start_process()
        p.terminate()
        await p.wait()
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE reservations SET expires_at=clock_timestamp()-INTERVAL '1 second' WHERE order_id=$1",
                o["id"],
            )
        p2, _ = await self.start_process()
        self.assertEqual(await self.balance(), (1, 0, 1))
        p2.terminate()
        await p2.wait()
        await self.start_process()
        self.assertEqual(await self.balance(), (1, 0, 1))
        await self.assert_invariants()
        await self.record(
            "Перезапуск с резервом",
            "1/0/1 при старте; повтор без изменений",
            str(await self.balance()),
        )

    async def test_11_crash_refund_failure_and_lost_response(self):
        """Останов после commit поздней оплаты; ошибка, потерянный ответ и сверка после перезапуска."""
        await self.supply(1)
        o, p = await self.new_order()
        await self.expire(o)
        server, base = await self.start_process("after_payment_commit")
        with self.assertRaises(aiohttp.ClientError):
            await self.process_post(base, f"shop/payments/{p['id']}/succeeded", 101, {})
        self.assertEqual(await asyncio.wait_for(server.wait(), 5), 86)
        async with self.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM refund_jobs"), 1)
            await c.execute(
                "INSERT INTO simulator_faults(payment_id,failures_left,lost_responses_left) VALUES ($1,1,1)",
                p["id"],
            )
        server, _ = await self.start_process()
        server.terminate()
        await server.wait()
        async with self.pool.acquire() as c:
            self.assertIn(
                "ошибка", await c.fetchval("SELECT last_error FROM refund_jobs")
            )
        await process_refunds(self.pool, force=True)
        async with self.pool.acquire() as c:
            self.assertIn(
                "потерян", await c.fetchval("SELECT last_error FROM refund_jobs")
            )
            self.assertEqual(
                await c.fetchval("SELECT count(*) FROM simulator_refunds"), 1
            )
            self.assertEqual(
                await c.fetchval(
                    "SELECT count(*) FROM finance_operations WHERE type='refund'"
                ),
                0,
            )
            await c.execute("UPDATE refund_jobs SET next_attempt_at=NOW()")
        await self.start_process()
        await process_refunds(self.pool, force=True)
        async with self.pool.acquire() as c:
            self.assertEqual(
                await c.fetchval("SELECT status FROM refund_jobs"), "succeeded"
            )
            self.assertEqual(
                await c.fetchval(
                    "SELECT count(*) FROM finance_operations WHERE type='receipt'"
                ),
                1,
            )
            self.assertEqual(
                await c.fetchval(
                    "SELECT count(*) FROM finance_operations WHERE type='refund'"
                ),
                1,
            )
        self.assertEqual(await self.balance(), (1, 0, 1))
        self.assertEqual((await finance.summary(self.pool, MANAGER))["cash_flow"], 0)
        await self.assert_invariants()
        await self.record(
            "Перезапуск возврата и потеря ответа",
            "1 receipt, 1 refund, 1 ответ провайдера; склад 1/0/1",
            "Совпало по таблицам PostgreSQL",
        )

    async def test_12_crash_inside_checkout(self):
        """Процесс остановлен до commit: нет половины заказа; тот же ключ повторяется успешно."""
        await self.supply(2)
        await cart(self.pool, 101, {1: 1})
        key = str(uuid4())
        server, base = await self.start_process("checkout_before_commit")
        data = {
            "recipient_name": "Учебный клиент",
            "pickup_method": "office",
            "request_id": key,
            "items": {"1": 1},
        }
        with self.assertRaises(aiohttp.ClientError):
            await self.process_post(base, "shop/checkout", 101, data)
        self.assertEqual(await asyncio.wait_for(server.wait(), 5), 86)
        async with self.pool.acquire() as c:
            self.assertEqual(await c.fetchval("SELECT count(*) FROM orders"), 0)
            self.assertEqual(await c.fetchval("SELECT count(*) FROM reservations"), 0)
        self.assertEqual(await self.balance(), (2, 0, 2))
        _, base = await self.start_process()
        status, d = await self.process_post(base, "shop/checkout", 101, data)
        self.assertEqual(status, 201, d)
        self.assertEqual(await self.balance(), (2, 1, 1))
        await self.assert_invariants()
        await self.record(
            "Останов до commit",
            "Откат; повтор тем же ключом: 2/1/1",
            str(await self.balance()),
        )

    async def test_13_roles_tampering_and_supply_idempotency(self):
        """Покупатель не меняет склад, чужие заказы, сумму или срок; поступление защищено UUID."""
        data = {
            "product_id": 1,
            "quantity": 1,
            "request_id": str(uuid4()),
            "description": "Квота",
        }
        self.assertEqual((await self.call("POST", "stock/supplies", 101, data))[0], 403)
        for _ in range(2):
            self.assertEqual(
                (await self.call("POST", "stock/supplies", data=data))[0], 200
            )
        self.assertEqual(await self.balance(), (1, 0, 1))
        o, p = await self.new_order()
        for path in (
            f"shop/orders/{o['public_number']}/cancel",
            f"shop/payments/{p['id']}/succeeded",
        ):
            self.assertEqual((await self.call("POST", path, 102, {}))[0], 400)
        for path, data2 in (
            (f"shop/payments/{p['id']}/succeeded", {"amount_kopecks": 1}),
            (f"shop/orders/{o['public_number']}/cancel", {"expires_at": "2099-01-01"}),
        ):
            self.assertEqual((await self.call("POST", path, 101, data2))[0], 400)
        self.assertEqual((await self.call("GET", "stock", 101))[0], 403)
        self.assertEqual((await self.call("GET", "shop/orders", 102))[1], [])
        self.assertEqual(await self.balance(), (1, 1, 0))
        self.assertEqual((await finance.summary(self.pool, MANAGER))["receipts"], 0)
        await self.assert_invariants()
        await self.record(
            "Права и подмена полей",
            "Отказ; склад 1/1/0, поступления 0",
            f"Склад {await self.balance()}, поступления 0",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
