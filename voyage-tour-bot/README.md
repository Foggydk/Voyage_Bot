# Вояж-тур — учебный Telegram-бот

Турагентство по Азии: туры для отдыха и шопинг-туры.
Сейчас работают меню, каталог, карточки, корзина, оформление заявки и «Мои заказы».
ИИ-помощник пока заготовка.

## Команды для Windows (PowerShell)

Откройте PowerShell и выполните по порядку.

```powershell
cd C:\Users\dkeyd\PycharmProjects\voyage-tour-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python src\bot.py
```

Если активация venv запрещена политикой:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Затем снова:

```powershell
.\.venv\Scripts\Activate.ps1
python src\bot.py
```

В Telegram найдите своего бота и отправьте `/start`. Остановка бота: `Ctrl+C`.

Токен берётся из `.env` (`BOT_TOKEN`). Не копируйте его в код и не публикуйте.

Для заявок нужен PostgreSQL. В `.env` укажите `DATABASE_URL` с реальным паролем
(не `YOUR_PASSWORD`), база `voyage_tour`. При запуске бот сам создаёт таблицы
и записывает каталог туров. Если URL не задан, меню и корзина работают,
а оформление заказа будет недоступно.
