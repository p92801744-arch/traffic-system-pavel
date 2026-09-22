# Ящик лидов — локальный кабинет

Личная лента заказов из Telegram. Открывается только на этом компьютере: `http://127.0.0.1:8787`.

## Запуск

1. Положи `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` в `apps/leads/.env` (страница [my.telegram.org/apps](https://my.telegram.org/apps)).
2. Установи зависимости и запусти:

```powershell
cd apps/leads
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app
```

3. Открой http://127.0.0.1:8787 и один раз подтверди вход кодом из Telegram.
4. Создай бота у [@BotFather](https://t.me/BotFather) (`/newbot`), положи токен в `BOT_TOKEN` и напиши ему `/start`. Новые заказы начнут приходить в этот чат. Старые из истории в бот не дублируются.

Оценщик включён всегда: на каждой карточке пишет, почему заявку отдал или нет. Ключ `LLM_API_KEY` не нужен.

## Что внутри

- `config/sources.yaml` — каналы, ключевые слова, стоп-слова
- `config/profile.yaml` — кого считать своим заказом
- `data/` — база SQLite и сессия Telegram, в git не попадают
