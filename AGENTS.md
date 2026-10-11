# Проект Павла — что здесь лежит

**Стиль общения с Павелом:** Open Steps ([opensteps.ai](https://opensteps.ai/)) — правило `.cursor/rules/open-steps-global.mdc`, навыки в `C:\Users\Павел\.cursor\open-steps\skills\`.

Две части в одном репозитории.

1. **Аудит рынка** — агенты собирают спрос, боли, конкурентов и новости по нише «AI для бизнеса» в РФ.
2. **Ящик лидов** — личная лента заказов из Telegram на этом компьютере: `http://127.0.0.1:8787`. В интернет её не выкладывать.

Сайт с услугами: https://pavelklyushin.ru. Заявки в Telegram: https://t.me/pasha_kvk.

## Кто такой Павел и что ему отдавать

Сбор данных широкий: все кластеры A–J в `data/semantic-clusters.json`.
Продажа узкая: интернет-магазины МСБ и кейс магазина аккумуляторов, без названия магазина и без ссылки на витрину.

Его услуги: агентские системы, автоматизация на n8n/Make, конвейер каталога, сайты, дообучение модели, боты и ИИ-ассистенты, связки и 1С, база знаний со ссылкой на источник, разбор документов в CRM или 1С, конвейер карточек Wildberries/Ozon, статьи в Дзен.

Не его: реклама и спам, самореклама исполнителя, живой помощник, курьер, SMM, монтаж, продажи, риелтор, разовый дизайн карточки, курс «заработай на боте», повтор одного и того же текста. Подробности — в `.cursor/rules/leads-inbox.mdc` и `.cursor/skills/leads-inbox/SKILL.md`.

## Карта файлов

| Путь | За что отвечает |
|---|---|
| `AGENTS.md` | Эта карта. Читать первым в новом пространстве. |
| `.cursor/rules/market-intel.mdc` | Как собирать рынок: куда класть файлы, лимиты, фит 1–5. |
| `.cursor/rules/leads-inbox.mdc` | Что класть в ящик лидов и что выбрасывать. |
| `.cursor/skills/leads-inbox/SKILL.md` | Тот же отбор, когда Павел присылает пример «это не моё». |
| `agents/01-demand.md` … `05-synthesis.md` | Промпты пяти агентов аудита. |
| `data/semantic-clusters.json` | Стартовые фразы для спроса. |
| `data/schemas/` | Форма json-ответов агентов. |
| `data/raw/` | Сырые сборы, файл с датой, не перезаписывать. |
| `data/reports/` | Готовые выводы аудита. |
| `runbooks/orchestrate.md` | Порядок запуска агентов. |
| `docs/mcp-wordstat-tools.md` | Как звать WordStat. |
| `scripts/` | Сбор болей и склейка сырых json. |
| `apps/leads/` | Сам ящик: сбор Telegram, фильтр, оценка, лента, бот. |
| `apps/leads/config/profile.yaml` | Услуги Павла и что ему не подходит. |
| `apps/leads/config/sources.yaml` | Чаты, ключевые слова, стоп-слова. |
| `apps/leads/app/filters.py` | Ворота: реклама, спам, чужие вакансии, повтор текста. |
| `apps/leads/app/scorer.py` | Оценка 1–5 и текст «почему отдал». |
| `apps/leads/data/reject-lessons.json` | Причины, которые Павел написал на «Не моё». |
| `apps/leads/.env.example` | Какие ключи нужны. Сами ключи только в `.env` на компьютере. |

На компьютере и не в git: `apps/leads/.env` (ключи Telegram и бота), файл сессии Telegram, база `leads.db`. Папка `rusakb-gate` — отдельный проект, репозиторий `rusakb-gate`, сюда не входит.

# Self-Agency Market Intel — оркестратор

Мультиагентная система аудита ниши **«AI для бизнеса»** на рынке РФ.

## Позиционирование

- **Сбор данных:** широко — все кластеры A–J в `data/semantic-clusters.json`
- **Продажа:** узко — интернет-магазины МСБ, кейс магазина аккумуляторов
- **Каналы:** YouTube (Pavel Klyushin | AI & Automation), Telegram (@pavel_AI_IT)

## Агенты

| # | Файл | Задача | Инструмент | Выход |
|---|------|--------|------------|-------|
| 1 | `agents/01-demand.md` | Спрос, частотность, тренды | `rusak-wordstat` MCP | `data/raw/demand-*.json` |
| 2 | `agents/02-pains.md` | Боли аудитории | Firecrawl search | `data/raw/pains-*.json` |
| 3 | `agents/03-competitors.md` | Конкуренты | Firecrawl agent + YouTube MCP | `data/raw/competitors-*.json` |
| 4 | `agents/04-narratives.md` | Нарративы и новости | Firecrawl news | `data/raw/narratives-*.json` |
| 5 | `agents/05-synthesis.md` | Синтез и рекомендации | Все raw-файлы | `data/reports/*.md` |

## Порядок запуска

1. Убедиться, что MCP `rusak-wordstat` и Firecrawl CLI аутентифицированы
2. Агент 1 → Агент 2 → Агент 3 → Агент 4 (можно 2–4 параллельно)
3. Агент 5 — только после наличия raw-данных от 1–4

Подробный чеклист: `runbooks/orchestrate.md`

## Быстрый старт (один агент)

```
Запусти Агента 1 по промпту agents/01-demand.md.
Используй seed-фразы из data/semantic-clusters.json (первые 5 фраз для теста).
Сохрани результат в data/raw/demand-YYYY-MM-DD.json
```

## Схемы данных

JSON Schema в `data/schemas/`:
- `demand-record.json`
- `pain-record.json`
- `competitor-record.json`
- `synthesis-output.json`

## Скрипты

- `scripts/run-pains.ps1` — Firecrawl-запросы для Агента 2 (Windows)
- `scripts/merge-results.js` — сводка всех raw-файлов в один JSON

## Документация

- WordStat MCP: `docs/mcp-wordstat-tools.md`
- Firecrawl auth: `firecrawl login --browser` или `FIRECRAWL_API_KEY` в `.env`

## Ящик лидов

Личная лента заказов. Запуск из `apps/leads`: `.\.venv\Scripts\python.exe -m app`, затем http://127.0.0.1:8787.

Оценка локальная, без внешнего чата. В ленту попадает заказ на услугу Павла. Реклама, спам и второй экземпляр того же текста не попадают. Если Павел жмёт «Не моё» и пишет причину, она сохраняется в `apps/leads/data/reject-lessons.json` и учитывается дальше.

Как устроен отбор: `apps/leads/README.md`.
