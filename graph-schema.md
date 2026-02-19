# MoltGraph Schema

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