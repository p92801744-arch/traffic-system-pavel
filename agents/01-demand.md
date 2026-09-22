# Агент 1 — Спрос (WordStat)

## Роль

Собрать данные о спросе по нише «AI для бизнеса» в РФ через MCP `rusak-wordstat`.

## Вход

- `data/semantic-clusters.json` — кластеры A–J с seed-фразами
- Схема: `data/schemas/demand-record.json`

## Инструмент

MCP-сервер `rusak-wordstat` (tool: `Call_yandex_Wordstat_Universal_`).

### Параметры tool (все обязательные)

```json
{
  "phrase": "ии для интернет магазина",
  "report_type_": "topRequests",
  "numPhrases_": 10,
  "regions": "225",
  "devices": "all"
}
```

`report_type_`: `topRequests` | `dynamics` | `regions`

## Алгоритм

1. Загрузить `data/semantic-clusters.json`
2. Выбрать фразы для прогона (по умолчанию: все; лимит **80 фраз** за один запуск)
3. Для каждой фразы:
   - Вызвать WordStat MCP
   - Извлечь: частотность, похожие запросы, регионы (если доступно)
   - Оценить `commercial_intent`: high / medium / low
   - Оценить `fit_score` (1–5): близость к e-commerce + автоматизация + измеримый результат
4. Сохранить массив записей в `data/raw/demand-YYYY-MM-DD.json`

## Формат выхода

```json
{
  "agent": "01-demand",
  "collected_at": "2026-08-06T20:00:00Z",
  "phrases_requested": 88,
  "phrases_collected": 85,
  "records": [
    {
      "cluster": "G",
      "phrase": "ии для интернет магазина",
      "frequency": 1200,
      "trend": "up",
      "related_phrases": [{"phrase": "...", "frequency": 500}],
      "regions": [{"region": "Москва", "share": 0.35}],
      "commercial_intent": "high",
      "fit_score": 5,
      "collected_at": "2026-08-06T20:01:00Z",
      "source": "rusak-wordstat-mcp"
    }
  ]
}
```

## Оценка commercial_intent

| Уровень | Признаки |
|---------|----------|
| high | «на заказ», «под ключ», «специалист», «внедрение», «настройка» |
| medium | «для бизнеса», «автоматизация», «как использовать» |
| low | «что такое», «бесплатно», «обучение», «курс» |

## Оценка fit_score

| Балл | Критерий |
|------|----------|
| 5 | E-commerce + автоматизация + измеримый KPI |
| 4 | E-commerce или сильная автоматизация для МСБ |
| 3 | Общий AI для бизнеса, применимо к магазинам |
| 2 | Enterprise / узкая отрасль без e-commerce |
| 1 | Не релевантно текущему позиционированию |

## Fallback

Если MCP недоступен — создать `data/raw/demand-manual.json` с пометкой `"source": "manual"` и инструкцией заполнить с wordstat.yandex.ru.

## Dry-run

Для теста достаточно **1 фразы** из кластера G: `ии для интернет магазина`.
