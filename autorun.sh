#!/usr/bin/env bash
set -euo pipefail

# --------- config ----------
PROJECT_DIR="/mnt/vault/kunal/social-data/Moltbook/"   # <-- change to folder with docker-compose.yml / compose.yml
SLEEP_SECONDS=600                      # 10 minutes
LOG_FILE="${PROJECT_DIR}/full_crawl_loop.log"
# ---------------------------

cd "$PROJECT_DIR"

echo "[start] $(date -Is) loop starting (sleep=${SLEEP_SECONDS}s)" | tee -a "$LOG_FILE"

while true; do
  echo "[run] $(date -Is) starting crawl" | tee -a "$LOG_FILE"

  # Run crawl; don't crash the loop if it fails
  if docker compose run --rm \
      -e MOLTBOOK_BASE_URL="https://www.moltbook.com/api/v1" \
      -e USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
      -e DEBUG_HTTP=1 \
      -e REQUESTS_PER_MINUTE=60 \
      -e POSTS_PAGE_SIZE=1000 \
      -e POSTS_MAX_PAGES=0 \
      -e FETCH_POST_DETAILS=1 \
      -e CRAWL_COMMENTS=1 \
      -e COMMENTS_FROM_POST_DETAILS=1 \
      -e COMMENTS_LIMIT_PER_POST=1000 \
      -e SUBMOLT_TOP_LIMIT=1000 \
      -e ENRICH_SUBMOLTS=1 \
      -e ENRICH_SUBMOLTS_LIMIT=1000 \
      -e CRAWL_SUBMOLT_FEEDS=1 \
      -e SUBMOLT_FEED_MAX_PAGES=1000 \
      -e FETCH_AGENT_PROFILES=1 \
      -e PROFILE_LIMIT=1000 \
      -e SUBMOLT_FEED_SORT=new \
      -e MODERATOR_SUBMOLTS_LIMIT=1000 \
      crawler python -m scripts.full_crawl >>"$LOG_FILE" 2>&1
  then
    echo "[ok]  $(date -Is) crawl finished" | tee -a "$LOG_FILE"
  else
    code=$?
    echo "[err] $(date -Is) crawl exited with code=$code (continuing)" | tee -a "$LOG_FILE"
  fi

  echo "[sleep] $(date -Is) sleeping ${SLEEP_SECONDS}s" | tee -a "$LOG_FILE"
  sleep "$SLEEP_SECONDS"
done
