# Neo4j Database Maintaining

## Backfill

- Sometimes agent accounts are not claimed, so they do not have a X account associated with them
- X account

```shell
docker compose run --rm   -e NEO4J_URI="bolt://neo4j:7687"   -e NEO4J_USER="neo4j"   -e NEO4J_PASSWORD="please-change-me"   -e MOLTBOOK_BASE_URL="https://www.moltbook.com/api/v1"   -e USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"   -e REQUESTS_PER_MINUTE=60   crawler python -m scripts.backfill.x_accounts --only-missing --limit 10000

```

- Reply comment

```shell
 docker compose run --rm   -e MOLTBOOK_BASE_URL="https://www.moltbook.com/api/v1"   -e USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"   -e REQUESTS_PER_MINUTE=30   -e MOLTBOOK_API_KEY="$MOLTBOOK_API_KEY"   crawler python -m scripts.backfill.post_comments   --post-id 216057c5-b441-4e70-80f6-2117214a29ea --limit 200
```

- Comments

```shell
docker compose run --rm   -e NEO4J_URI="bolt://neo4j:7687"   -e NEO4J_USER="neo4j"   -e NEO4J_PASSWORD="please-change-me"   -e MOLTBOOK_BASE_URL="https://www.moltbook.com/api/v1"   -e USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"   -e REQUESTS_PER_MINUTE=50   -e MOLTBOOK_API_KEY="$MOLTBOOK_API_KEY"   crawler python -m scripts.backfill.comments --limit-posts 500 --min-missing 1 --max-comments 500 --prefer-post-details --mark
```

- Is Spam and Verified

```shell
docker compose run --rm \
  -e NEO4J_URI="bolt://neo4j:7687" \
  -e NEO4J_USER="neo4j" \
  -e NEO4J_PASSWORD="please-change-me" \
  -e MOLTBOOK_BASE_URL="https://www.moltbook.com/api/v1" \
  -e MOLTBOOK_API_KEY="$MOLTBOOK_API_KEY" \
  -e USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
  -e REQUESTS_PER_MINUTE=20 \
  crawler python -m scripts.backfill_post_comment_moderation \
    --limit-posts 1000 \
    --only-missing \
    --mark
```

- Deleted Account/Post/Comment/Submolt

```shell
docker compose run --rm \
  -e NEO4J_URI="bolt://neo4j:7687" \
  -e NEO4J_USER="neo4j" \
  -e NEO4J_PASSWORD="please-change-me" \
  -e MOLTBOOK_BASE_URL="https://www.moltbook.com/api/v1" \
  -e MOLTBOOK_API_KEY="$MOLTBOOK_API_KEY" \
  -e USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
  -e REQUESTS_PER_MINUTE=20 \
  crawler python -m scripts.backfill_deleted_flags_clean \
    --limit-agents 5000 \
    --limit-submolts 2000 \
    --limit-posts 10000 \
    --max-comments 500
```