# Self-Agency Market Intel

Мультиагентная система аудита ниши **«AI для бизнеса»** на рынке РФ.

## Быстрый старт

1. **WordStat MCP** — уже в `~/.cursor/mcp.json` как `rusak-wordstat`. Обновите MCP в Cursor.
2. **Firecrawl** — `firecrawl login --browser` или скопируйте `.env.example` → `.env` и укажите `FIRECRAWL_API_KEY`.
3. Откройте `AGENTS.md` и следуйте `runbooks/orchestrate.md`.

## Структура

```
AGENTS.md        — карта проекта, читать первым
.cursor/rules/   — правила аудита рынка и ящика лидов
.cursor/skills/  — навык отбора заявок
agents/          — промпты агентов 01–05
apps/leads/      — ящик заказов из Telegram, только на этом компьютере
data/
  semantic-clusters.json  — 88 seed-фраз (кластеры A–J)
  schemas/       — JSON Schema для выходов
  raw/           — сырые данные агентов
  reports/       — финальные отчёты синтеза
scripts/         — run-pains.ps1, merge-results.js
docs/            — документация MCP
```

## Агенты

| # | Задача | Инструмент |
|---|--------|------------|
| 1 | Спрос | rusak-wordstat MCP |
| 2 | Боли | Firecrawl |
| 3 | Конкуренты | Firecrawl + YouTube MCP |
| 4 | Нарративы | Firecrawl news |
| 5 | Синтез | Все raw-файлы |

## Позиционирование

- **Сбор:** широко (AI для бизнеса)
- **Продажа:** узко (интернет-магазины МСБ)
