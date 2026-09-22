# WordStat MCP (rusak-wordstat)

MCP-сервер на n8n (`rusakbn8n.ru`) с инструментами Яндекс API.

## Подключение

В `C:\Users\Павел\.cursor\mcp.json`:

```json
"rusak-wordstat": {
  "url": "https://rusakbn8n.ru/mcp/7cedfdaa-7858-4f86-83fa-81ce9a63758d"
}
```

**Сервер в Cursor:** `user-rusak-wordstat`  
**Статус:** ready

## Доступные tools

| Tool | Назначение |
|------|------------|
| `Call_yandex_Wordstat_Universal_` | Частотность, динамика, регионы WordStat |
| `Call_andex_API_Generativ_Search_` | AI-ответ из Яндекс Поиска |
| `Call_yandex_API_Image_Search_` | Поиск изображений |
| `Call_yandex_API_Image_Search_1` | Веб-поиск через Yandex Search API |
| `Call_yandex_pars_URL_` | Парсинг текста со страницы по URL |

## Call_yandex_Wordstat_Universal_

**Основной tool для Агента 1.**

### Параметры (все обязательные)

| Параметр | Тип | Описание |
|----------|-----|----------|
| `phrase` | string | Ключевая фраза |
| `report_type_` | string | `topRequests` (по умолчанию), `dynamics`, `regions` |
| `numPhrases_` | number | Количество фраз в ответе |
| `regions` | string | ID или название региона (напр. `225`, `Москва`) |
| `devices` | string | `desktop`, `mobile`, `all` |

### Пример вызова

```json
{
  "phrase": "ии для интернет магазина",
  "report_type_": "topRequests",
  "numPhrases_": 10,
  "regions": "225",
  "devices": "all"
}
```

### Встроенный workflow (из описания tool)

Tool автоматически классифицирует ключи по интенту:
- **Commercial:** купить, цена, заказать, стоимость
- **Informational:** как, почему, отзывы, обзор
- **Service/Geo:** спб, москва, ремонт, сервис

## Проверка (2026-08-09)

| Tool | Статус |
|------|--------|
| MCP сервер `user-rusak-wordstat` | OK (ready, authenticated) |
| `Call_andex_API_Generativ_Search_` | OK |
| `Call_yandex_API_Image_Search_1` | OK (отвечает; по факту отдаёт image URLs) |
| `Call_yandex_Wordstat_Universal_` | **FAIL** — `Bad request - please check your parameters` |

Проверены комбинации: `regions` = `225`, `213`, `1`, `0`, `""`, `Россия`; `report_type_` = `topRequests`, `dynamics`, `regions`; `devices` = `all`, `desktop`, `mobile`. Все варианты — та же ошибка.

Ошибка приходит **из n8n workflow** при обращении к Yandex Wordstat API (MCP-транспорт работает).

**Что проверить на rusakbn8n.ru:**
1. Токен Yandex Direct / Wordstat API (не истёк ли)
2. Ноду HTTP в workflow Wordstat — mapping параметров `phrase`, `report_type_`, `regions`
3. Логи execution в n8n для failed run (там будет точный ответ Yandex API)

**Fallback:** ручной ввод в `data/raw/demand-manual.json` с wordstat.yandex.ru

## Использование в Агенте 1

1. Читать `data/semantic-clusters.json`
2. Для каждой фразы вызывать `Call_yandex_Wordstat_Universal_`
3. Сохранять в `data/raw/demand-YYYY-MM-DD.json` по схеме `data/schemas/demand-record.json`
