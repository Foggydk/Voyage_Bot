"""Записывает две демонстрации реального HTTP API и Mini App на временных базах."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import aiohttp
from playwright.async_api import async_playwright, expect

from tests.support import MANAGER, TestDatabase, auth


@asynccontextmanager
async def server(db, port=0, failpoint="", reservation_seconds=120):
    env = {
        **os.environ,
        "TEST_DATABASE_URL": db.dsn,
        "MANAGER_TELEGRAM_ID": str(MANAGER),
        "RESERVATION_SECONDS": str(reservation_seconds),
        "TEST_MODE": "true",
        "TEST_FAILPOINT": failpoint,
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.server",
        "--port",
        str(port),
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        ready = await asyncio.wait_for(proc.stdout.readline(), 20)
        if not ready.startswith(b"READY "):
            raise RuntimeError("Тестовый сервер не запустился")
        yield int(ready.split()[1]), proc
    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()


async def request(port, user, path, payload, method="POST"):
    url = f"http://127.0.0.1:{port}/api/shop/{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, headers={"Authorization": auth(user)}, json=payload
        ) as response:
            response.raise_for_status()
            return await response.json()


async def order(port, user, name):
    items = {"1": 1}
    await request(port, user, "cart", {"items": items}, "PUT")
    data = await request(
        port,
        user,
        "checkout",
        {
            "recipient_name": name,
            "items": items,
            "pickup_method": "office",
            "request_id": str(uuid4()),
        },
    )
    payment = await request(port, user, f"orders/{data['public_number']}/payment", {})
    return data["public_number"], payment["id"]


async def caption(page, text):
    await page.evaluate(
        """text => {
        let note = document.querySelector('#demo-caption') || document.createElement('aside');
        note.id = 'demo-caption'; note.textContent = text;
        Object.assign(note.style, {position:'fixed', bottom:'12px',left:'4%', width:'92%',boxSizing:'border-box',
            background:'#142e34',color:'white',padding:'13px 20px',borderRadius:'8px',
            font:'19px/1.4 system-ui',zIndex:'10000',pointerEvents:'none'});
        (document.querySelector('dialog[open]') || document.body).append(note);
    }""",
        text,
    )
    await asyncio.sleep(3.1)


async def refresh(page):
    async with page.expect_response(lambda r: "/api/summary?" in r.url):
        await page.locator("#refresh").click()
    await expect(page.locator("#message")).to_have_text("Данные обновлены.")


async def record(browser, folder, scenario):
    db = TestDatabase()
    await db.open()
    try:
        await db.pool.execute(
            "UPDATE products SET price=1200,cost_price=700 WHERE id=1"
        )
        with TemporaryDirectory() as raw:
            context = await browser.new_context(
                viewport={"width": 1100, "height": 840},
                locale="ru-RU",
                record_video_dir=raw,
                record_video_size={"width": 1100, "height": 840},
            )
            try:
                sdk = (
                    "window.Telegram={WebApp:{initData:"
                    + json.dumps(auth(MANAGER)[4:])
                    + ",ready(){},expand(){}}};"
                )
                await context.route(
                    "https://telegram.org/js/telegram-web-app.js",
                    lambda route: route.fulfill(
                        content_type="text/javascript", body=sdk
                    ),
                )
                page = await context.new_page()
                async with server(
                    db, reservation_seconds=10 if scenario == "stock" else 120
                ) as (port, _):
                    await page.goto(f"http://127.0.0.1:{port}/")
                    await expect(
                        page.locator("#supply-product option").first
                    ).to_have_value("1")
                    await page.get_by_role("button", name="Склад", exact=True).click()
                    await page.locator("#supply-quantity").fill("1")
                    await page.locator("#supply-description").fill(
                        "Учебная квота на Пхукет"
                    )
                    await page.locator("#supply-submit").click()
                    await expect(page.locator("#message")).to_contain_text(
                        "Поступление записано"
                    )
                    item = page.locator("#stock-balances article").first
                    await item.scroll_into_view_if_needed()
                    await caption(
                        page,
                        "Учебный стенд · Менеджер добавил одно место на Пхукет. На складе 1, в резерве 0, доступно 1.",
                    )
                    number, payment = await order(port, 101, "Покупатель А")
                    await refresh(page)
                    await page.get_by_role("button", name="Заказы", exact=True).click()
                    await page.locator("#order-list button").first.click()
                    await caption(
                        page,
                        f"{number} · Платёж #{payment} ожидает подтверждения. Место в резерве, поступлений пока нет.",
                    )
                    await page.locator("#close-order").click()
                    if scenario == "finance":
                        await request(port, 101, f"payments/{payment}/succeeded", {})
                        await refresh(page)
                        note = await db.pool.fetchval(
                            "SELECT text FROM notification_outbox WHERE chat_id=$1",
                            MANAGER,
                        )
                        assert number in note
                        await caption(
                            page,
                            "Оплата подтверждена. В очереди уведомлений менеджера: "
                            + " ".join(note.splitlines()[1:]),
                        )
                        await page.get_by_role(
                            "button", name="Расход", exact=True
                        ).click()
                        await page.locator("#expense-amount").fill("100")
                        await page.locator("#expense-description").fill(
                            "Трансфер для клиента"
                        )
                        await page.locator("#expense-submit").click()
                        await expect(page.locator("#message")).to_contain_text(
                            "Расход сохранён"
                        )
                        await page.locator(
                            "#operation-list"
                        ).scroll_into_view_if_needed()
                        await caption(
                            page,
                            "Расход на трансфер 100 ₽. Поступление 1 200 ₽ и расход видны в журнале.",
                        )
                        await page.locator("#cash").scroll_into_view_if_needed()
                        await expect(page.locator("#result")).to_contain_text("400,00")
                        await caption(
                            page,
                            "Денежный поток 1 100 ₽. Себестоимость тура 700 ₽, учебный результат 400 ₽.",
                        )
                        await page.get_by_role(
                            "button", name="Заказы", exact=True
                        ).click()
                        await page.locator("#order-list button").first.click()
                        await page.get_by_role(
                            "button", name="Начать полный учебный возврат"
                        ).click()
                        await expect(
                            page.get_by_role(
                                "button", name="Имитировать успешный возврат"
                            )
                        ).to_be_visible()
                        await caption(
                            page,
                            "Возврат запрошен. До подтверждения деньги и проданное место остаются учтёнными.",
                        )
                        await page.get_by_role(
                            "button", name="Имитировать успешный возврат"
                        ).click()
                        await expect(page.locator("#cash")).to_contain_text("-100,00")
                        await page.locator("#close-order").click()
                        await page.locator("#cash").scroll_into_view_if_needed()
                        await caption(
                            page,
                            "Возврат подтверждён. Место вернулось на склад, поток и результат равны −100 ₽ из-за трансфера.",
                        )
                    else:
                        await page.get_by_role(
                            "button", name="Склад", exact=True
                        ).click()
                        await caption(
                            page,
                            "На стенде срок резерва 10 секунд. Ждём, пока сервер освободит место покупателя А.",
                        )
                        async with asyncio.timeout(25):
                            while (
                                await db.pool.fetchval(
                                    "SELECT status FROM orders WHERE public_number=$1",
                                    number,
                                )
                                != "expired"
                            ):
                                await asyncio.sleep(0.5)
                        number_b, payment_b = await order(port, 102, "Покупатель Б")
                        await request(port, 102, f"payments/{payment_b}/succeeded", {})
                        await refresh(page)
                        await page.get_by_role(
                            "button", name="Склад", exact=True
                        ).click()
                        await item.scroll_into_view_if_needed()
                        await caption(
                            page,
                            f"Срок резерва А истёк. Б купил последнее место по заказу {number_b}. Остаток 0/0/0.",
                        )
                if scenario == "stock":
                    async with server(db, port, "after_payment_commit") as (_, proc):
                        try:
                            await request(
                                port, 101, f"payments/{payment}/succeeded", {}
                            )
                        except aiohttp.ClientConnectionError:
                            pass
                        assert await asyncio.wait_for(proc.wait(), 5) == 86
                    assert (
                        await db.pool.fetchval(
                            "SELECT count(*) FROM refund_jobs WHERE status='pending'"
                        )
                        == 1
                    )
                    await page.locator("#refresh").click()
                    await expect(page.locator("#message")).to_contain_text("Нет связи")
                    await caption(
                        page,
                        "Пришла поздняя оплата А. Сервер остановлен после записи платежа и задания возврата. Задание сохранено в PostgreSQL.",
                    )
                    async with server(db, port):
                        await refresh(page)
                        await expect(page.locator("#result")).to_contain_text("500,00")
                        await page.locator(
                            "#stock-refunds"
                        ).scroll_into_view_if_needed()
                        await caption(
                            page,
                            "Сервер перезапущен. Сохранённое задание завершено, деньги А возвращены один раз. Место осталось у Б.",
                        )
                        await page.get_by_role(
                            "button", name="Финансы", exact=True
                        ).click()
                        await page.locator(
                            "#operation-list"
                        ).scroll_into_view_if_needed()
                        await caption(
                            page,
                            "Два поступления по 1 200 ₽ и один возврат. Поток 1 200 ₽, себестоимость только продажи Б — 700 ₽, результат 500 ₽.",
                        )
                video = page.video
            finally:
                await context.close()
            await video.save_as(folder / f"{scenario}.webm")
            print(f"VIDEO_OK {scenario}", flush=True)
    finally:
        await db.close()


async def main():
    folder = Path(__file__).resolve().parents[1] / "docs" / "demo"
    folder.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        try:
            for scenario in ("finance", "stock"):
                await record(browser, folder, scenario)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
