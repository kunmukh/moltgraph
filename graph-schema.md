# MoltGraph Schema

This crawler maps Moltbook entities to Neo4j nodes and relationships. Most nodes and edges are **temporal**: we track when they were first and last observed by the crawler. In addition to core crawl fields, several backfill scripts add **deletion**, **moderation**, **comment-backfill**, and **owner-X enrichment** fields. These are documented below.

## Node Types

### `(:Agent {name})` *(unique by name)*
Captured from:
- Post / Comment `author`
- Moderator lists
- `/agents/profile?name=...` enrichment
- Authenticated `get_me()` bootstrap

Common properties:
- `name` *(string, unique)*
- `id` *(uuid if available)*
- `display_name`
- `description`
- `avatar_url`
- `karma`
- `follower_count`, `following_count`
- `is_claimed`, `is_active`, `is_deleted`
- `created_at`, `last_active`, `updated_at`
- `deleted_at`
- `deletion_reason`
- `owner_twitter_handle` *(canonicalized owner X handle when available)*
- `profile_last_fetched_at` *(set when fetched via `/agents/profile` or owner-X backfill)*
- `profile_last_fetch_status` *(e.g., `ok`, `no_owner`, `no_x_handle`, `deleted_404`)*
- `profile_last_fetch_error_code` *(HTTP/error code if applicable)*
- `first_seen_at`, `last_seen_at` *(crawler observation timestamps)*

Notes:
- Agent-owner/X enrichment may come from the Moltbook profile payload or from HTML profile scraping/backfill.
- On 404 during profile fetch, agents are retained but marked deleted via `is_deleted`, `updated_at`, `deleted_at`, and `deletion_reason`.

### `(:Submolt {name})` *(unique by name)*
Captured from:
- `/submolts`
- `/submolts/{name}` enrichment
- Post payloads (`post.submolt`)

Common properties:
- `name` *(unique)*
- `display_name`
- `description`
- `avatar_url`
- `banner_url`
- `banner_color`, `theme_color`
- `subscriber_count`, `post_count`
- `is_deleted`
- `created_at`, `updated_at`
- `deleted_at`
- `deletion_reason`
- `first_seen_at`, `last_seen_at`

Notes:
- The crawl writes submolts from the top-level listing, optional per-submolt enrichment, and discovery from post payloads.
- A deletion backfill can later set `is_deleted`, `updated_at`, `deleted_at`, and `deletion_reason`.
### `(:Post {id})` *(unique by id)*
Captured from:
- `/posts`
- `/submolts/{name}/feed`
- `/posts/{id}` detail fetches
- Authenticated feed snapshots

Common properties:
- `id` *(uuid, unique)*
- `title`, `content`
- `type` *(e.g., text / link)*
- `score`, `upvotes`, `downvotes`
- `comment_count`, `hot_score`
- `is_pinned`, `is_locked`, `is_deleted`, `is_spam`
- `verification_status`
- `created_at`, `updated_at`
- `deleted_at`
- `deletion_reason`
- `submolt` *(string name, normalized from object before storage)*
- `submolt_id` *(if available)*
- `moderation_backfilled_at`
- `moderation_backfill_status` *(e.g., `ok`, `error`, `post_empty`, `post_deleted_404`, `comments_deleted_404`)*
- `moderation_comments_seen`
- `moderation_comments_updated`
- `comments_backfilled_at`
- `comments_backfill_status` *(e.g., `ok`, `empty`, `deleted_404`)*
- `comments_backfill_expected`
- `comments_backfill_got_before`
- `comments_backfill_fetched`
- `first_seen_at`, `last_seen_at`

Notes:
- The crawler normalizes `post.submolt` to a plain submolt name before upsert. 
- The moderation backfill writes `is_spam`, `verification_status`, and moderation audit fields.
- The missing-comments backfill writes comment-backfill audit fields.
- If a post endpoint 404s during backfill, the post is retained and marked deleted using `is_deleted`, `updated_at`, `last_seen_at`, `deleted_at`, and `deletion_reason`.

### `(:Comment {id})` *(unique by id)*
Captured from:
- `/posts/{id}/comments`
- Nested `post.comments` trees from `/posts/{id}` when available

Common properties:
- `id` *(uuid, unique)*
- `post_id`
- `content`
- `score`, `upvotes`, `downvotes`
- `reply_count`, `depth`
- `is_deleted`, `is_spam`
- `verification_status`
- `created_at`, `updated_at`
- `deleted_at`
- `deletion_reason`
- `moderation_backfilled_at`
- `first_seen_at`, `last_seen_at`

Notes:
- Comment payloads are normalized for API shape drift (`createdAt` → `created_at`, `updatedAt` → `updated_at`, `replyCount` → `reply_count`). `author_name` / `authorName` may also appear in payloads for author resolution, while canonical authorship is represented by `(:Agent)-[:AUTHORED]->(:Comment)`.
- The moderation backfill writes `is_spam`, `verification_status`, and `moderation_backfilled_at`.
- If a parent post 404s, previously stored comments under that post are marked deleted using `is_deleted`, `updated_at`, `last_seen_at`, `deleted_at`, and `deletion_reason`.

### `(:Crawl {id})` *(unique by id)*
Represents one crawl run.

Common properties:
- `id` *(e.g., `full:<uuid>`)*
- `mode` *(e.g., `full`, `weekly`, `smoke`)*
- `cutoff` *(datetime cutoff for “before now” semantics)*
- `started_at`, `ended_at`, `last_updated_at`

Checkpoint semantics:
- The crawler also maintains per-crawl checkpoints via `set_checkpoint(...)`.
- Observed checkpoint keys in the current crawl implementation include:
  - `posts_offset_<sort>_<time-or-na>`
  - `submolt_feed_offset_<submolt_name>`
- The exact storage form of these checkpoint keys is `Neo4jStore` implementation-specific, but they are part of the crawl-state schema consumed by the crawler.

### `(:FeedSnapshot {id})` *(unique by id)*
Represents a feed snapshot at a time.

Common properties:
- `id` *(typically `<crawl_id>:<sort>`)*
- `sort` *(e.g., `hot`)*
- `observed_at`
- `first_seen_at`, `last_seen_at`

Notes:
- The current full crawl explicitly writes an authenticated `hot` feed snapshot.

### `(:XAccount {handle})` *(unique by handle)*
Captured from:
- Agent owner information in `/agents/profile`
- `x_accounts.py` backfill
- Optional HTML profile scraping

Common properties:
- `handle` *(unique, normalized lowercase without `@`)*
- `url`
- `name`
- `avatar_url`
- `bio`
- `follower_count`, `following_count`
- `is_verified`
- `first_seen_at`, `last_seen_at`

Notes:
- XAccount enrichment is **not** only HTML-based anymore; it is also populated from `agent.owner.x_handle` and related owner metadata.

---

## Edge Types

### `(a:Agent)-[:AUTHORED]->(p:Post|c:Comment)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at` *(copied from post/comment when available)*

### `(p:Post)-[:IN_SUBMOLT]->(s:Submolt)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at` *(copied from post when available)*

### `(c:Comment)-[:ON_POST]->(p:Post)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at` *(copied from comment when available)*

### `(c:Comment)-[:REPLY_TO]->(parent:Comment)`
Properties:
- `first_seen_at`, `last_seen_at`
- `created_at`

### `(a:Agent)-[:MODERATES]->(s:Submolt)`
Properties:
- `first_seen_at`, `last_seen_at`
- `role`
- `ended_at` *(set when a moderator is no longer present in a refresh)*

Notes:
- Moderators are refreshed from `/submolts/{name}/moderators`, then the corresponding agent nodes are also upserted / enriched.

### `(a:Agent)-[:SIMILAR_TO {source}]->(b:Agent)`
Properties:
- `source` *(currently `html_profile`)*
- `first_seen_at`, `last_seen_at`
- `ended_at` *(set when similarity no longer appears)*

Notes:
- Similar-agent links are currently produced by optional HTML profile scraping.

### `(a:Agent)-[:HAS_OWNER_X]->(x:XAccount)`
Properties:
- `first_seen_at`, `last_seen_at`

Notes:
- This edge may be populated from profile-owner metadata or HTML scraping.

### `(fs:FeedSnapshot)-[:CONTAINS]->(p:Post)`
Properties:
- `first_seen_at`, `last_seen_at`
- `rank` *(position in feed at snapshot time)*

---

## Temporal Graph Semantics

We model graph evolution using time bounds:

- `first_seen_at`: when a node or edge was first observed by the crawler
- `last_seen_at`: last time the node or edge was observed still present
- `ended_at`: when an edge such as `MODERATES` or `SIMILAR_TO` disappeared
- `deleted_at`: when a backfill inferred that an entity had disappeared (commonly from API 404)
- `updated_at`: platform-provided update time when available, or backfill observation time when deletion is inferred
- `Crawl.cutoff`: “as of” boundary for each crawl run
- `FeedSnapshot.observed_at`: timestamped snapshot of recommendations

This enables analyses such as:
- Which moderators changed over time?
- When did an agent become linked to an X account?
- When was a post or comment inferred deleted?
- How did hot-feed ranking evolve across crawls?
- Which posts required large comment backfills, and how complete were they?
