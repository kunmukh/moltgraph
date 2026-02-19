# MoltGraph: Moltbook Social Network Graph

This repo crawls the Moltbook network (agents, submolts, posts, comments, feed snapshots, and optional UI-only enrichments) and stores it as a temporal graph in **Neo4j**. 

It supports:
- **Smoke test** (≈30s) to validate API + Neo4j writes end-to-end  
- **Full crawl** (one-time historical ingest up to “now”)  
- **Weekly crawl** (incremental updates since last crawl cutoff)  
- Temporal evolution via `first_seen_at`, `last_seen_at`, `ended_at`, and crawl/feed snapshots

---

## Repo Layout

```
.
├── docker-compose.yml              # Neo4j + crawler services
├── credentials.json                # (local) creds (keep secret)
├── moltbook-registration
│   ├── bot_register.txt            # notes / registration info
│   ├── query.json                  # debugging artifacts
│   └── response.jso
└── crawler/
    ├── Dockerfile                  # crawler container image
    ├── requirements.txt            # python deps
    ├── moltbook_client.py          # Moltbook API client (rate limit + retries)
    ├── neo4j_store.py              # Neo4j schema + upsert logic
    ├── html_scrape.py              # optional UI-only scrape (similar agents + owner X)
    ├── cypher/
    │   └── schema.cypher           # constraints + indexes
    └── scripts/
        ├── init_db.py              # applies schema.cypher
        ├── smoke_test.py           # 30s end-to-end validation
        ├── full_crawl.py           # one-time full ingest
        └── weekly_crawl.py         # weekly incremental ingest
```

---

## Requirements

- Docker + Docker Compose
- A Moltbook API key (`MOLTBOOK_API_KEY`)
- Ports open (locally):
  - Neo4j Browser: `7474`
  - Bolt: `7687`

---

## Setup

### 1) Create `.env` (repo root)

Create a `.env` file in the repo root (same directory as `docker-compose.yml`):

```bash
# Moltbook
MOLTBOOK_API_KEY=YOUR_KEY_HERE
MOLTBOOK_BASE_URL=https://www.moltbook.com/api/v1

# Neo4j (inside Docker network)
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change_me

# Crawler behavior
REQUESTS_PER_MINUTE=60
USER_AGENT=MoltGraphCrawler/0.1

# Optional toggles
FETCH_POST_DETAILS=0
SCRAPE_AGENT_HTML=0
CRAWL_COMMENTS=1
COMMENTS_LIMIT_PER_POST=500

# Full crawl submolt enrichment (expensive)
ENRICH_SUBMOLTS=1

# Agent profile refresh controls (weekly)
PROFILE_REFRESH_DAYS=7
PROFILE_REFRESH_LIMIT=500
```

**Notes**
- `REQUESTS_PER_MINUTE` controls client-side throttling.
- `FETCH_POST_DETAILS=1` calls `/posts/:id` for each post (slower).
- `SCRAPE_AGENT_HTML=1` enables UI-only scraping (slower / brittle).
- `ENRICH_SUBMOLTS=1` can be very expensive for large numbers of submolts.

---

## Run Neo4j

```bash
docker compose up -d neo4j
```

Neo4j Browser:
- http://localhost:7474

Login:
- user: `neo4j`
- password: `NEO4J_PASSWORD`

---

## Initialize Schema (constraints + indexes)

Apply `crawler/cypher/schema.cypher`:

```bash
docker compose run --rm crawler python -m scripts.init_db
```

Verify in Neo4j Browser:

```cypher
SHOW CONSTRAINTS;
SHOW INDEXES;
```

---

## Smoke Test (≈30 seconds)

Run:

```bash
docker compose run --rm crawler python -m scripts.smoke_test
```

Smoke test validates:
- Moltbook API connectivity
- Neo4j connectivity/writes
- Ingestion of at least `Agent`, `Post`, `Submolt` (and `Comment` if available)
- Relationships: `AUTHORED`, `IN_SUBMOLT`, `ON_POST`

Verify counts:

```cypher
MATCH (n) RETURN labels(n) AS label, count(*) AS cnt ORDER BY cnt DESC;
MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC;
```

---

## Full Crawl (One-Time)

A full crawl ingests “everything discoverable” up to the crawl cutoff (UTC now).

### Recommended run

```bash
docker compose run --rm \
  -e REQUESTS_PER_MINUTE=60 \
  -e ENRICH_SUBMOLTS=0 \
  -e CRAWL_COMMENTS=1 \
  crawler python -m scripts.full_crawl
```

### Faster first full crawl (no comments)

```bash
docker compose run --rm \
  -e CRAWL_COMMENTS=0 \
  crawler python -m scripts.full_crawl
```

### Track progress while running

The crawl writes a `:Crawl` node with checkpoints:

```cypher
MATCH (cr:Crawl)
RETURN cr.id, cr.mode, cr.started_at, cr.submolts_offset, cr.posts_offset, cr.last_updated_at
ORDER BY cr.started_at DESC
LIMIT 5;
```

---

## Weekly Crawl (Incremental)

Weekly crawl:
- Refreshes Submolts + Moderators
- Fetches new posts since `last_cutoff`
- Updates comments for new posts (optional)
- Profile-refreshes newly seen agents (and optionally stale agents)

Run:

```bash
docker compose run --rm crawler python -m scripts.weekly_crawl
```

Optional tuning:

```bash
docker compose run --rm \
  -e REQUESTS_PER_MINUTE=60 \
  -e CRAWL_COMMENTS=1 \
  -e PROFILE_REFRESH_DAYS=7 \
  -e PROFILE_REFRESH_LIMIT=500 \
  crawler python -m scripts.weekly_crawl
```

---

## Moltbook → Neo4j Graph Model

This crawler maps Moltbook entities to Neo4j nodes and relationships. Most nodes/edges are **temporal**: we track when they were first and last observed by the crawler.

### Node Types

#### `(:Agent {name})` *(unique by name)*
Captured from:
- Post/Comment `author`
- Moderator lists
- `/agents/profile?name=...` enrichment

Common properties:
- `name` *(string, unique)*
- `id` *(uuid if available)*
- `display_name`
- `description`
- `avatar_url`
- `karma`
- `follower_count`, `following_count`
- `is_claimed`, `is_active`
- `created_at`, `last_active`, `updated_at`
- `profile_last_fetched_at` *(set only when fetched via /agents/profile)*
- `first_seen_at`, `last_seen_at` *(crawler observation timestamps)*

#### `(:Submolt {name})` *(unique by name)*
Common properties:
- `name` *(unique)*
- `display_name`
- `description`
- `avatar_url`
- `banner_url`
- `banner_color`, `theme_color`
- `subscriber_count`, `post_count`
- `created_at`, `updated_at`
- `first_seen_at`, `last_seen_at`

#### `(:Post {id})` *(unique by id)*
Common properties:
- `id` *(uuid, unique)*
- `title`, `content`
- `type` *(e.g., text/link)*
- `score`, `upvotes`, `downvotes`
- `comment_count`, `hot_score`
- `is_pinned`, `is_locked`, `is_deleted`
- `created_at`, `updated_at`
- `submolt` *(string name, normalized from object)*
- `submolt_id` *(if available)*
- `first_seen_at`, `last_seen_at`

#### `(:Comment {id})` *(unique by id)*
Common properties:
- `id` *(uuid, unique)*
- `post_id`
- `content`
- `score`, `upvotes`, `downvotes`
- `reply_count`, `depth`
- `is_deleted`
- `created_at`, `updated_at`
- `first_seen_at`, `last_seen_at`

#### `(:Crawl {id})` *(unique by id)*
Represents one crawl run.

Common properties:
- `id` (`full:...` or `weekly:...`)
- `mode` (`smoke`, `full`, `weekly`)
- `cutoff` *(datetime cutoff for “before now” semantics)*
- `started_at`, `ended_at`, `last_updated_at`
- `submolts_offset`, `posts_offset` *(checkpoints)*

#### `(:FeedSnapshot {id})` *(unique by id)*
Represents a feed snapshot at a time.

Common properties:
- `id` (`<crawl_id>:<sort>`)
- `sort` (e.g., `hot`)
- `observed_at`
- `first_seen_at`, `last_seen_at`

#### `(:XAccount {handle})` *(unique by handle)*
Optional UI-only enrichment from HTML scraping.

Common properties:
- `handle` *(unique)*
- `url`
- `first_seen_at`, `last_seen_at`

---

### Edge Types

#### `(a:Agent)-[:AUTHORED]->(p:Post|c:Comment)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at` *(copied from post/comment)*

#### `(p:Post)-[:IN_SUBMOLT]->(s:Submolt)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at` *(copied from post)*

#### `(c:Comment)-[:ON_POST]->(p:Post)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at` *(copied from comment)*

#### `(c:Comment)-[:REPLY_TO]->(parent:Comment)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at`

#### `(a:Agent)-[:MODERATES]->(s:Submolt)`
Properties:
- `first_seen_at`, `last_seen_at`
- `role`
- `ended_at` *(set when a moderator is no longer present in a refresh)*

#### `(a:Agent)-[:SIMILAR_TO {source}]->(b:Agent)` *(optional UI-only)*
Properties:
- `source` (default: `html_profile`)
- `first_seen_at`, `last_seen_at`
- `ended_at` *(ended when similarity no longer present)*

#### `(a:Agent)-[:HAS_OWNER_X]->(x:XAccount)` *(optional UI-only)*
Properties:
- `first_seen_at`, `last_seen_at`

#### `(fs:FeedSnapshot)-[:CONTAINS]->(p:Post)`
Properties:
- `first_seen_at`, `last_seen_at`
- `rank` *(position in feed at snapshot time)*

---

## Temporal Graph Semantics

We model “graph evolution” using time bounds:

- `first_seen_at`: when a node/edge was first observed by the crawler
- `last_seen_at`: last time the node/edge was observed still present
- `ended_at`: (for edges like `MODERATES`, `SIMILAR_TO`) when it disappeared
- `Crawl.cutoff`: “as of” boundary for each crawl run
- `FeedSnapshot.observed_at`: timestamped snapshot of recommendations

This enables analyses like:
- “Which moderators changed over time?”
- “When did an agent become similar to another?”
- “How did the hot feed ranking evolve week by week?”

---

## Notes / Caveats

- Moltbook endpoints may rate-limit or occasionally return 502/503/504; the client includes retries + exponential backoff.
- HTML scraping is brittle by nature (UI changes may break parsing). Use it only if you need Similar/Owner-X edges.
- Full enrichment of all submolts/posts can be expensive; prefer staged enrichment.

---

## Cite This Repo

If you use this crawler in academic work, please cite it.

```bibtex
@software{mukherjee_moltbook_neo4j_crawler_2026,
  author       = {Mukherjee, Kunal},
  title        = {Moltbook → Neo4j Graph Crawler},
  year         = {2026},
  month        = {2},
  version      = {0.1},
  note         = {GitHub repository},
  url          = {<REPO_URL>}
}
```

---

## License

MIT.
