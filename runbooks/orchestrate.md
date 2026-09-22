# Orchestrate — запуск всех агентов

Пошаговый чеклист полного аудита ниши «AI для бизнеса» (РФ).

## Предварительные условия

- [ ] MCP `rusak-wordstat` подключён и активен в Cursor (Settings → Tools & MCP)
- [ ] Firecrawl CLI установлен: `firecrawl --version`
- [ ] Firecrawl аутентифицирован: `firecrawl login --browser` или `FIRECRAWL_API_KEY` в `.env`
- [ ] YouTube MCP подключён (для Агента 3)

## Шаг 1 — Агент 1: Спрос (WordStat)

**Промпт:** `agents/01-demand.md`

```
Запусти Агента 1. Прочитай data/semantic-clusters.json.
Для каждой seed-фразы вызови rusak-wordstat MCP.
Сохрани в data/raw/demand-YYYY-MM-DD.json
```

**Лимит:** 80 фраз  
**Время:** ~30–60 мин (зависит от rate limit n8n)  
**Dry-run:** 1 фраза `ии для интернет магазина`

## Шаг 2 — Агент 2: Боли (Firecrawl)

**Промпт:** `agents/02-pains.md`  
**Скрипт:** `scripts/run-pains.ps1`

```powershell
cd C:\Users\Павел\Desktop\Pavel
.\scripts\run-pains.ps1
```

Или в Cursor Agent:
```
Запусти Агента 2 по agents/02-pains.md
```

**Выход:** `data/raw/pains-YYYY-MM-DD.json`

## Шаг 3 — Агент 3: Конкуренты

**Промпт:** `agents/03-competitors.md`

```
Запусти Агента 3. YouTube search (max 3 запроса) + Firecrawl scrape.
Сохрани в data/raw/competitors-YYYY-MM-DD.json
```

**Лимит:** YouTube search ≤ 5, Firecrawl agent `--max-credits 50`

## Шаг 4 — Агент 4: Нарративы

**Промпт:** `agents/04-narratives.md`

```powershell
firecrawl search "ai бизнес россия" --sources news --tbs qdr:m -o data/raw/narratives-news-YYYY-MM-DD.json --json
```

Повторить для всех 7 запросов из промпта.

## Шаг 5 — Сводка raw-данных

```powershell
node scripts/merge-results.js
```

**Выход:** `data/raw/_merged-YYYY-MM-DD.json`

## Шаг 6 — Агент 5: Синтез

**Промпт:** `agents/05-synthesis.md`

```
Запусти Агента 5. Прочитай все data/raw/*.json.
Сгенерируй отчёты в data/reports/
```

**Выход:**
- `data/reports/vectors.md`
- `data/reports/gaps.md`
- `data/reports/positioning.md`
- `data/reports/content-plan.md`
- `data/reports/synthesis-YYYY-MM-DD.json`

## Параллелизация

Агенты 2, 3, 4 можно запускать **параллельно** после завершения Агента 1 (или независимо, если demand не нужен для уточнения запросов).

```
Agent1 (WordStat)
    ↓
Agent2 (Pains) ──┐
Agent3 (Competitors) ──┼──→ Agent5 (Synthesis)
Agent4 (Narratives) ──┘
```

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| WordStat MCP не отвечает | Проверить rusakbn8n.ru, обновить MCP в Cursor |
| Firecrawl not authenticated | `firecrawl login --browser` |
| YouTube quota exceeded | Использовать только `youtube_search_suggestions` |
| Пустой raw-файл | Проверить логи, повторить с 1 запросом (dry-run) |

## Расписание (рекомендация)

| Частота | Агент |
|---------|-------|
| Раз в 2 недели | Agent 1 (топ-20 фраз) |
| Раз в неделю | Agent 4 (news) |
| Раз в месяц | Agents 2, 3, 5 (полный цикл) |
