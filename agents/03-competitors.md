# Агент 3 — Конкуренты

## Роль

Картировать конкурентов в нише «AI/автоматизация для бизнеса» в РФ: агентства, фрилансеры, YouTube-каналы.

## Вход

- Схема: `data/schemas/competitor-record.json`

## Инструменты

### Firecrawl Agent (сайты агентств)

```bash
firecrawl agent "извлеки услуги, позиционирование и цены" \
  --urls "https://example.com" \
  --schema-file data/schemas/competitor-record.json \
  --max-credits 50 \
  --wait -o data/raw/competitors-sites-YYYY-MM-DD.json
```

### YouTube MCP (экономно)

- `youtube_search_suggestions` — бесплатно, для SEO-идей
- `youtube_search` — **100 quota/запрос**, использовать максимум 3–5 раз

Запросы для search:
1. `ии для бизнеса`
2. `автоматизация бизнеса n8n`
3. `ai агент для бизнеса`

Для каждого канала из топ-5 результатов:
- `youtube_get_channel` — описание, подписчики
- Зафиксировать: positioning, services (из описания), content_gaps

## Стартовый список для анализа (Firecrawl scrape)

Сайты и каналы для ручного/автоматического сбора (дополнять по мере обнаружения):

- vc.ru/tag/ai
- Страницы услуг digital-агентств с AI/автоматизацией
- YouTube-каналы по n8n, Make, AI для бизнеса (русскоязычные)

## Алгоритм

1. YouTube search (3 запроса) → список каналов-конкурентов
2. Firecrawl scrape страниц услуг (5–10 URL)
3. Для каждого конкурента заполнить:
   - `name`, `channel_or_site`, `type`
   - `services[]` — что продают
   - `positioning` — как себя позиционируют (1–2 предложения)
   - `price_hint` — если есть публичные цены
   - `content_gaps` — что делают шаблонно или не закрывают
4. Сохранить в `data/raw/competitors-YYYY-MM-DD.json`

## Формат выхода

```json
{
  "agent": "03-competitors",
  "collected_at": "2026-08-06T20:00:00Z",
  "records": [
    {
      "name": "Пример Агентство",
      "channel_or_site": "https://example.com",
      "type": "agency",
      "services": ["чат-боты", "внедрение CRM", "AI-консалтинг"],
      "positioning": "AI-трансформация для среднего бизнеса",
      "price_hint": "от 150 000 ₽",
      "content_gaps": ["нет кейсов e-commerce", "общие обещания без цифр"],
      "collected_at": "2026-08-06T20:10:00Z"
    }
  ]
}
```

## content_gaps — что искать

- Нет реальных кейсов с цифрами
- Продают «ChatGPT для всего»
- Нет связки маркетинг + автоматизация
- Не работают с МСБ / малыми чеками
- Нет e-commerce специализации

## Лимиты

- YouTube search: ≤ 5 запросов
- Firecrawl agent: `--max-credits 50`
- Целевой объём: 15–25 конкурентов

## Dry-run

Один `youtube_search_suggestions` по `ии для бизнеса` + scrape одной страницы услуг.
