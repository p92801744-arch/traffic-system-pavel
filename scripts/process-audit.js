#!/usr/bin/env node
/**
 * Process raw Firecrawl + Yandex AI data into structured agent outputs.
 * Usage: node scripts/process-audit.js
 */

const fs = require("fs");
const path = require("path");

const RAW = path.join(__dirname, "..", "data", "raw");
const DATE = "2026-08-06";

const PAIN_KEYWORDS = [
  "проблем", "боль", "сложн", "дорог", "не получ", "не работ", "ошибк",
  "не хватает", "сопротивлен", "обжигал", "разочаров", "непонятн",
  "страх", "риск", "не окуп", "потер", "ручн", "хаос", "нет специалист",
];

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(path.join(RAW, file), "utf8"));
  } catch {
    return null;
  }
}

function writeJson(name, data) {
  const p = path.join(RAW, name);
  fs.writeFileSync(p, JSON.stringify(data, null, 2), "utf8");
  console.log(`Wrote ${p}`);
}

function extractPains() {
  const files = fs.readdirSync(RAW).filter((f) => f.startsWith("pains-") && f.endsWith(".json") && !f.includes("dryrun"));
  const records = [];
  const seen = new Set();

  for (const file of files) {
    const data = readJson(file);
    const web = data?.data?.web || [];
    const platform = file.includes("vc") ? "vc.ru" : file.includes("habr") ? "habr" : "other";
    const topic = file.replace("pains-", "").replace(`-${DATE}.json`, "");

    for (const item of web) {
      const text = [item.title, item.description, item.markdown].filter(Boolean).join(" ");
      const lower = text.toLowerCase();
      const hasPain = PAIN_KEYWORDS.some((k) => lower.includes(k));

      if (!hasPain && !item.markdown) continue;

      const sentences = (item.markdown || item.description || item.title || "")
        .split(/[.!?\n]/)
        .map((s) => s.trim())
        .filter((s) => s.length > 40 && PAIN_KEYWORDS.some((k) => s.toLowerCase().includes(k)));

      const quote = sentences[0] || item.description || item.title;
      const key = `${item.url}|${quote.slice(0, 80)}`;
      if (seen.has(key)) continue;
      seen.add(key);

      let segment = "smb";
      if (lower.includes("интернет") || lower.includes("магазин") || lower.includes("e-commerce")) segment = "ecommerce";
      if (lower.includes("enterprise") || lower.includes("корпорат")) segment = "enterprise";

      records.push({
        source_url: item.url || "",
        title: item.title || "",
        pain_quote: quote.slice(0, 500),
        audience_segment: segment,
        topic_cluster: topic,
        platform,
        collected_at: new Date().toISOString(),
      });
    }
  }

  return {
    agent: "02-pains",
    collected_at: new Date().toISOString(),
    files_processed: files.length,
    records,
  };
}

function extractNarratives() {
  const files = fs.readdirSync(RAW).filter((f) => f.startsWith("narratives-") && f.endsWith(".json"));
  const records = [];

  for (const file of files) {
    const data = readJson(file);
    const news = data?.data?.news || [];
    const topic = file.replace("narratives-", "").replace(`-${DATE}.json`, "");

    for (const item of news) {
      const lower = (item.title + item.snippet).toLowerCase();
      let relevance = "low";
      if (lower.includes("business") || lower.includes("ecommerce") || lower.includes("мсб") || lower.includes("магазин") || lower.includes("seo") || lower.includes("automat")) relevance = "medium";
      if (lower.includes("росси") || lower.includes("russia") || lower.includes("малый") || lower.includes("smb")) relevance = "high";

      records.push({
        narrative: item.title,
        snippet: item.snippet,
        source: item.url,
        date: item.date || "",
        relevance_to_positioning: relevance,
        topic,
        collected_at: new Date().toISOString(),
      });
    }
  }

  return {
    agent: "04-narratives",
    collected_at: new Date().toISOString(),
    files_processed: files.length,
    records,
    summary: {
      top_narratives: [
        "AI-агенты и автономные системы для бизнеса",
        "GEO / генеративная оптимизация как новый SEO",
        "No-code автоматизация (n8n, Make) для МСБ",
        "Дефицит ИТ-кадров для внедрения AI в России",
        "E-commerce: автоматизация контента и заказов",
        "Импортозамещение и российские AI-решения",
        "Уход с маркетплейсов на свой сайт + органика",
      ],
    },
  };
}

function extractCompetitors() {
  const data = readJson(`competitors-agencies-${DATE}.json`);
  const web = data?.data?.web || [];
  const records = [];

  const yandexCompetitors = [
    { name: "Владимир Карпухин", channel_or_site: "YouTube", type: "youtube", services: ["n8n", "MCP", "RAG", "AI-агенты"], positioning: "Глубокие технические гайды по n8n и AI", content_gaps: ["мало e-commerce кейсов", "фокус на технику, не на бизнес-результат"] },
    { name: "AI Мастерская", channel_or_site: "YouTube", type: "youtube", services: ["AI-видео", "автопостинг", "HeyGen"], positioning: "AI-контент на автопилоте", content_gaps: ["не про e-commerce процессы", "контент без цифр ROI"] },
    { name: "НейроЦех", channel_or_site: "YouTube", type: "youtube", services: ["воркфлоу", "анализ комментариев", "контент-стратегия"], positioning: "Реальные n8n воркфлоу", content_gaps: ["нет специализации на магазинах"] },
    { name: "Action Plan | Николай Хлебинский", channel_or_site: "YouTube", type: "youtube", services: ["контент-заводы", "e-commerce", "маркетплейсы"], positioning: "E-commerce и маркетплейсы без большой команды", content_gaps: ["мало про автоматизацию заказов/CRM"] },
    { name: "Breakout AI", channel_or_site: "YouTube", type: "youtube", services: ["n8n", "ChatGPT", "Telegram-боты"], positioning: "Для новичков, бесплатный n8n", content_gaps: ["поверхностно", "нет B2B кейсов"] },
    { name: "Академия n8n AI", channel_or_site: "Telegram", type: "telegram", services: ["шаблоны n8n", "обучение", "AI-агенты"], positioning: "1500+ шаблонов, тренажёр", content_gaps: ["курсовая модель", "не под ключ"] },
    { name: "Vibe Business", channel_or_site: "Telegram", type: "telegram", services: ["контент-завод", "RAG", "RSS"], positioning: "Реалити по контент-заводу", content_gaps: ["не про интернет-магазины"] },
    { name: "Neuro Automata", channel_or_site: "Telegram", type: "telegram", services: ["n8n уроки", "воркфлоу"], positioning: "Пошаговые разборы n8n", content_gaps: ["нет e-commerce специализации"] },
  ];

  for (const c of yandexCompetitors) {
    records.push({ ...c, collected_at: new Date().toISOString(), source: "yandex_ai" });
  }

  for (const item of web.slice(0, 15)) {
    const text = [item.title, item.description, item.markdown].filter(Boolean).join(" ");
    if (!text.toLowerCase().match(/ии|ai|автоматиз|нейро|n8n|чат.?бот/)) continue;

    records.push({
      name: item.title?.slice(0, 80) || "Unknown",
      channel_or_site: item.url || "",
      type: "agency",
      services: extractServices(text),
      positioning: item.description?.slice(0, 200) || "",
      price_hint: extractPrice(text),
      content_gaps: ["требует ручной проверки"],
      collected_at: new Date().toISOString(),
      source: "firecrawl",
    });
  }

  return {
    agent: "03-competitors",
    collected_at: new Date().toISOString(),
    records,
  };
}

function extractServices(text) {
  const services = [];
  const map = {
    "чат-бот": "чат-боты",
    "chatgpt": "ChatGPT интеграции",
    "n8n": "n8n автоматизация",
    "crm": "CRM интеграции",
    "seo": "SEO",
    "контент": "AI-контент",
    "rag": "RAG / база знаний",
    "агент": "AI-агенты",
    "маркетплейс": "маркетплейсы",
    "интернет-магазин": "e-commerce",
  };
  const lower = text.toLowerCase();
  for (const [k, v] of Object.entries(map)) {
    if (lower.includes(k) && !services.includes(v)) services.push(v);
  }
  return services.length ? services : ["AI для бизнеса"];
}

function extractPrice(text) {
  const m = text.match(/от\s+[\d\s]+(?:\s*₽|руб)/i);
  return m ? m[0] : "";
}

function buildDemand() {
  const clusters = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "data", "semantic-clusters.json"), "utf8")
  );

  const demandSignals = {
    A: { trend: "up", commercial_intent: "medium", fit_score: 3, note: "Верх воронки, высокий охват для YouTube" },
    B: { trend: "up", commercial_intent: "high", fit_score: 4, note: "Сильный коммерческий хвост (n8n, no-code)" },
    C: { trend: "up", commercial_intent: "high", fit_score: 3, note: "Хайп AI-агентов, высокая конкуренция в объяснении" },
    D: { trend: "up", commercial_intent: "medium", fit_score: 5, note: "GEO + SEO + контент — ключевой вектор для e-commerce" },
    E: { trend: "stable", commercial_intent: "high", fit_score: 3, note: "CRM автоматизация, фрагментированный рынок" },
    F: { trend: "stable", commercial_intent: "medium", fit_score: 2, note: "Аналитика — вторичный вектор" },
    G: { trend: "up", commercial_intent: "high", fit_score: 5, note: "Главный продажный кластер — интернет-магазины" },
    H: { trend: "up", commercial_intent: "medium", fit_score: 3, note: "RAG для внутренних процессов, сложно продать без кейса" },
    I: { trend: "stable", commercial_intent: "high", fit_score: 4, note: "Прямой коммерческий спрос на подрядчиков" },
    J: { trend: "up", commercial_intent: "low", fit_score: 4, note: "Контент-воронка: боли и вопросы → доверие" },
  };

  const records = [];
  for (const cluster of clusters.clusters) {
    const signal = demandSignals[cluster.id] || {};
    for (const phrase of cluster.phrases) {
      let intent = signal.commercial_intent || "medium";
      const lower = phrase.toLowerCase();
      if (/заказать|под ключ|специалист|настройка|сколько стоит/.test(lower)) intent = "high";
      if (/как|стоит ли|ошибки|безопасность|заменит/.test(lower)) intent = "low";

      let fit = signal.fit_score || 3;
      if (/интернет.?магазин|карточк|wildberries|ozon|seo|geo/.test(lower)) fit = 5;
      if (/n8n|автоматизац/.test(lower)) fit = Math.max(fit, 4);

      records.push({
        cluster: cluster.id,
        cluster_name: cluster.name,
        phrase,
        frequency: null,
        trend: signal.trend || "stable",
        commercial_intent: intent,
        fit_score: fit,
        analyst_note: signal.note || "",
        collected_at: new Date().toISOString(),
        source: "expert_assessment_wordstat_unavailable",
      });
    }
  }

  return {
    agent: "01-demand",
    collected_at: new Date().toISOString(),
    status: "wordstat_mcp_error",
    wordstat_error: "Call_yandex_Wordstat_Universal_ returns Bad request — требуется исправление n8n workflow",
    phrases_requested: records.length,
    phrases_collected: records.length,
    yandex_ai_insights: {
      in_demand_services: [
        "AI-агенты под ключ",
        "Комплексная интеграция AI (e-commerce, маркетинг, поддержка)",
        "Автоматизация документооборота",
        "RPA + AI интеграция",
        "AI-сотрудник вместо ботов",
      ],
      growing_vectors: [
        "AI-агенты для внутренних процессов",
        "GEO / генеративная оптимизация",
        "No-code автоматизация (n8n)",
        "AI-контент для e-commerce (карточки, статьи)",
        "Автоматизация заказов МСБ",
      ],
    },
    records,
  };
}

// Main
const pains = extractPains();
const narratives = extractNarratives();
const competitors = extractCompetitors();
const demand = buildDemand();

writeJson(`pains-${DATE}.json`, pains);
writeJson(`narratives-${DATE}.json`, narratives);
writeJson(`competitors-${DATE}.json`, competitors);
writeJson(`demand-${DATE}.json`, demand);

console.log(`\nPains: ${pains.records.length}`);
console.log(`Narratives: ${narratives.records.length}`);
console.log(`Competitors: ${competitors.records.length}`);
console.log(`Demand: ${demand.records.length}`);
