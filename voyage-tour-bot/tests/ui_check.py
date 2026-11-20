"""Браузерная приёмка Mini App против настоящего API и отдельной PostgreSQL."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from src.services import orders, payments
from src.web import create_app
from tests.support import MANAGER, TOKEN, TestDatabase, auth, cart


async def main():
    output = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    db = TestDatabase()
    with patch.dict(os.environ, {"MANAGER_TELEGRAM_ID": str(MANAGER)}):
        pool = await db.open()
        server = TestServer(create_app(pool, TOKEN))
        await server.start_server()
        errors = []
        try:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE products SET price=1200,cost_price=700")
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel="chrome", headless=True)
                context = await browser.new_context(
                    viewport={"width": 1200, "height": 900}, locale="ru-RU"
                )
                # Подменён только SDK Telegram с подписанной учебной личностью;
                # авторизация API и вся бизнес-логика настоящие.
                sdk = (
                    "window.Telegram={WebApp:{initData:"
                    + json.dumps(auth(MANAGER)[4:])
                    + ",ready(){},expand(){}}};"
                )
                await context.route(
                    "https://telegram.org/js/telegram-web-app.js",
                    lambda route: route.fulfill(
                        status=200, content_type="text/javascript", body=sdk
                    ),
                )
                page = await context.new_page()
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.goto(str(server.make_url("/")))
                await expect(page.locator("#cash")).to_contain_text("0,00")
                await page.get_by_role("button", name="Склад", exact=True).click()
                await page.locator("#supply-quantity").fill("3")
                await page.locator("#supply-description").fill(
                    "Учебная квота туроператора"
                )
                await page.locator("#supply-submit").click()
                await expect(page.locator("#message")).to_contain_text(
                    "Поступление записано"
                )
                user = await cart(pool, 101, {1: 1})
                order = await orders.create_order(
                    pool, user, "Учебный покупатель А", "office"
                )
                payment = await payments.start_payment(pool, 101, order.public_number)
                await payments.simulate_success(pool, 101, payment.id)
                user_b = await cart(pool, 102, {1: 1})
                await orders.create_order(
                    pool, user_b, "Учебный покупатель Б", "office"
                )
                await page.locator("#refresh").click()
                await expect(page.locator("#cash")).to_contain_text("1 200,00")
                await page.get_by_role("button", name="Расход", exact=True).click()
                await page.locator("#expense-amount").fill("100,00")
                await page.locator("#expense-description").fill("Трансфер для клиента")
                await page.locator("#expense-submit").click()
                await expect(page.locator("#message")).to_contain_text(
                    "Расход сохранён"
                )
                await expect(page.locator("#cash")).to_contain_text("1 100,00")
                await expect(page.locator("#result")).to_contain_text("400,00")
                await page.screenshot(
                    path=str(output / "finance-desktop.png"), full_page=True
                )
                await page.get_by_role("button", name="Заказы", exact=True).click()
                await page.get_by_role("button", name=order.public_number).click()
                await page.get_by_role(
                    "button", name="Начать полный учебный возврат"
                ).click()
                await expect(
                    page.get_by_role("button", name="Имитировать успешный возврат")
                ).to_be_visible()
                await expect(page.locator("#cash")).to_contain_text("1 100,00")
                await page.get_by_role(
                    "button", name="Имитировать успешный возврат"
                ).click()
                await expect(page.locator("#cash")).to_contain_text("-100,00")
                await page.locator("#close-order").click()
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.get_by_role("button", name="Склад", exact=True).click()
                await page.screenshot(
                    path=str(output / "stock-mobile.png"), full_page=True
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ), "Горизонтальное переполнение на телефоне"
                await context.set_offline(True)
                await page.locator("#refresh").click()
                await expect(page.locator("#message")).to_contain_text(
                    "Нет связи с сервером"
                )
                await context.set_offline(False)
                await page.locator("#refresh").click()
                await expect(page.locator("#message")).to_contain_text(
                    "Данные обновлены"
                )
                assert not errors, errors
                await browser.close()
            (output.parent / "ui-verification.md").write_text(
                "# Проверка интерфейса\n\n"
                "Команда: `python -m tests.ui_check`. Результат: OK.\n\n"
                "Chrome headless, настольный экран 1200×900 и мобильный 390×844.\n"
                "Отдельная PostgreSQL, настоящие API и подпись учебной личности Telegram.\n"
                "Проверено: поступление через форму; заказ и платёж; расход 100 ₽; поток 1100 ₽ и результат 400 ₽;\n"
                "возврат в два шага; поток −100 ₽; мобильный склад без горизонтального переполнения;\n"
                "ошибка при отключении сети и успешное обновление после восстановления; ошибок JavaScript нет.\n\n"
                "SDK Telegram заменён только в тестовом браузере. Это не проверка запуска из реального Telegram.\n"
                "Скриншоты содержат только учебные данные: `screenshots/finance-desktop.png`, `screenshots/stock-mobile.png`.\n",
                encoding="utf-8",
            )
            print(
                "UI_OK: supply, payment, expense, refund, mobile layout, offline recovery, no JavaScript errors"
            )
        finally:
            await server.close()
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
