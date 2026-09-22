#!/usr/bin/env node
/**
 * Merge all data/raw/*.json into a single summary file.
 * Usage: node scripts/merge-results.js
 */

const fs = require("fs");
const path = require("path");

const RAW_DIR = path.join(__dirname, "..", "data", "raw");
const DATE = new Date().toISOString().slice(0, 10);

function loadJsonFiles(dir) {
  if (!fs.existsSync(dir)) {
    console.error(`Directory not found: ${dir}`);
    process.exit(1);
  }

  const files = fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".json") && !f.startsWith("_merged"));

  const loaded = [];

  for (const file of files) {
    const filePath = path.join(dir, file);
    try {
      const content = JSON.parse(fs.readFileSync(filePath, "utf8"));
      loaded.push({ file, data: content });
    } catch (err) {
      console.warn(`Skip ${file}: ${err.message}`);
    }
  }

  return loaded;
}

function summarize(loaded) {
  const summary = {
    merged_at: new Date().toISOString(),
    files_count: loaded.length,
    agents: {},
    total_records: 0,
  };

  for (const { file, data } of loaded) {
    const agent = data.agent || inferAgent(file);
    if (!summary.agents[agent]) {
      summary.agents[agent] = { files: [], record_count: 0 };
    }

    const records = data.records || data.data || [];
    const count = Array.isArray(records) ? records.length : 1;

    summary.agents[agent].files.push(file);
    summary.agents[agent].record_count += count;
    summary.total_records += count;
  }

  return summary;
}

function inferAgent(filename) {
  if (filename.startsWith("demand")) return "01-demand";
  if (filename.startsWith("pains")) return "02-pains";
  if (filename.startsWith("competitors")) return "03-competitors";
  if (filename.startsWith("narratives")) return "04-narratives";
  return "unknown";
}

function main() {
  const loaded = loadJsonFiles(RAW_DIR);
  const summary = summarize(loaded);

  const output = {
    ...summary,
    files: loaded.map(({ file, data }) => ({ file, data })),
  };

  const outPath = path.join(RAW_DIR, `_merged-${DATE}.json`);
  fs.writeFileSync(outPath, JSON.stringify(output, null, 2), "utf8");

  console.log(`Merged ${loaded.length} files -> ${outPath}`);
  console.log(`Total records: ${summary.total_records}`);
  for (const [agent, info] of Object.entries(summary.agents)) {
    console.log(`  ${agent}: ${info.record_count} records (${info.files.length} files)`);
  }
}

main();
