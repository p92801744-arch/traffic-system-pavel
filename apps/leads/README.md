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

Один раз после входа в Windows поднимаются сайт, живая лента Telegram, сбор с бирж, встречи и бот. Если процесс падает, Планировщик поднимает его снова через минуту, а причина пишется в `logs\supervisor.log`. Установщик перед этим сам закрывает уже запущенный `python -m app` этого ящика, даже если он запущен системным Python, а не из `.venv`. Если порт 8787 занят чем-то другим, установка останавливается и пишет, кем. Секреты остаются в `apps/leads/.env` и в файле сессии на этом компьютере, в git они не попадают.

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

## Отклики из командной строки

Для агента и для ручной работы, без браузера. Запускать из `apps\leads`, автозапуск можно не останавливать: команда сама попросит у слушателя сессию Telegram и вернёт её.

```powershell
cd C:\Users\Павел\Desktop\Pavel\apps\leads
# новые заявки без отклика, JSON с полным текстом, источником, ссылкой и автором
.\.venv\Scripts\python.exe -m app.outreach list-new --limit 20
.\.venv\Scripts\python.exe -m app.outreach list-new --source telegram --min-fit 3 --since-hours 24

# отправить отклик в Telegram (текст в файле UTF-8, так кириллица не ломается)
.\.venv\Scripts\python.exe -m app.outreach send 123 --text-file reply.txt --dry-run
.\.venv\Scripts\python.exe -m app.outreach send 123 --text-file reply.txt            # auto: личка, если есть контакт, иначе тред
.\.venv\Scripts\python.exe -m app.outreach send 123 --text-file reply.txt --mode thread

# отклик ушёл на бирже (Kwork, HH, Авито) — отметить в базе
.\.venv\Scripts\python.exe -m app.outreach mark-replied 123 --text-file reply.txt --via kwork
```

В базе: `outreach_state` (sent, marked, error), `outreach_text`, `outreach_via`, `outreach_at`, статус заявки становится `contacted`. Лог отправок — таблица `outreach_log`.
Защита: одна заявка — один отклик, одному контакту не чаще раза в 7 дней, пауза 90 с, не больше 12 в час и 40 в сутки, после FloodWait — пауза, сколько попросил Telegram. Настройки — раздел `outreach` в `config/profile.yaml`.
Код выхода 0 — успех, 2 — отказ (причина в JSON `error`).

Кейсы для откликов: `docs/cases/smm_cases.md` (SMM, канал команды) и `docs/cases/cases.md` (ИИ, автоматизация, сайты).
