set -euo pipefail

CSV_FILE="${1:-resources/adLikelyUrls.csv}"
OUTPUT_FILE="${2:-results}"
MAX_RUNS="${3:-0}"

RUN_CMD="python cli.py --url {url} --timeout 60 --output-dir results -d ads,requests,cookies,screenshot,fingerprints,cmp --cmp-action in --anti-bot"

if [ ! -f "$CSV_FILE" ]; then
  echo "CSV file not found: $CSV_FILE" >&2
  exit 1
fi

run_count=0

tail -n +2 "$CSV_FILE" | while IFS=, read -r idx domain rest; do
  if [ "$MAX_RUNS" -gt 0 ] && [ "$run_count" -ge "$MAX_RUNS" ]; then
    break
  fi

  domain=$(echo "$domain" | sed -e 's/^ *//' -e 's/ *$//')
  if [ -z "$domain" ]; then
    continue
  fi

  cmd=${RUN_CMD//\{url\}/$domain}
  echo ">> $cmd"
  eval "$cmd"

  run_count=$((run_count + 1))
done

echo "Ran $run_count sites (limit: $MAX_RUNS)."