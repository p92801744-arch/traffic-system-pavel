#!/usr/bin/env bash
# Firecrawl queries for Agent 2 (Pains) — Linux/macOS
# Usage: ./scripts/run-agent.sh [YYYY-MM-DD]

set -euo pipefail
cd "$(dirname "$0")/.."

DATE="${1:-$(date +%Y-%m-%d)}"
OUT_DIR="data/raw"
mkdir -p "$OUT_DIR"

run_search() {
  local name="$1"
  local query="$2"
  local outfile="$OUT_DIR/pains-${name}-${DATE}.json"
  echo "Searching: $query -> $outfile"
  firecrawl search "$query" --scrape -o "$outfile" --json
}

run_search "vc-ii" '"внедрение ии" site:vc.ru'
run_search "automation-problems" '"автоматизация бизнеса" проблемы'
run_search "habr-ii" '"ии для бизнеса" site:habr.com'
run_search "vc-automation" '"автоматизация" предприниматель site:vc.ru'
run_search "chatgpt-business" '"chatgpt для бизнеса" опыт'
run_search "ecommerce-automation" '"интернет магазин" автоматизация проблемы'
run_search "habr-n8n" '"n8n" внедрение site:habr.com'

echo "Done. Merge with: node scripts/merge-results.js"
