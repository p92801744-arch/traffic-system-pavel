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

Карточка показывает, откуда заказ, оценку, короткую причину и ссылку. Кнопки: «Черновик», «Не моё» (бот спросит причину), «Позже», «Готово». С 22:00 до 08:00 по Москве карточки копятся и уходят утром, вместе со сводкой. Команды: `/help`, `/menu`, `/status`, `/stats`. Если сборщик замолкает или Telegram просит войти заново, бот пишет об этом. Меню команд появляется само после запуска бота.

Оценщик включён всегда: на каждой карточке пишет, почему заявку отдал или нет. Ключ `LLM_API_KEY` не нужен.

## Автозапуск на Windows

Один раз после входа в Windows поднимаются сайт, живая лента Telegram, сбор с бирж, встречи и бот. Если процесс падает, он запускается снова. Секреты остаются в `apps/leads/.env` и в файле сессии на этом компьютере, в git они не попадают.

```powershell
cd C:\Users\Павел\Desktop\Pavel
git pull
cd apps\leads
powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
```

Проверка:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1
```

Страница: http://127.0.0.1:8787. Журналы: `apps\leads\logs`.

Снять автозапуск:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-autostart.ps1
```

Пока автозапуск работает, второй `python -m app` не поднимай: замок не даст запустить две копии.

## Что внутри

- `config/sources.yaml` — каналы, ключевые слова, стоп-слова
- `config/profile.yaml` — кого считать своим заказом
- `data/` — база SQLite и сессия Telegram, в git не попадают
