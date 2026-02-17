import os
import uuid
from datetime import datetime, timezone

from moltbook_client import MoltbookClient
from neo4j_store import Neo4jStore

def iso_now():
    return datetime.now(timezone.utc).isoformat()

def pick_post_with_comments(posts):
    # Prefer a post with comments so we test comment ingestion too
    for p in posts:
        if (p.get("comment_count") or 0) > 0:
            return p
    return posts[0] if posts else None

def main():
    client = MoltbookClient()
    store = Neo4jStore(
        os.environ["NEO4J_URI"],
        os.environ["NEO4J_USER"],
        os.environ["NEO4J_PASSWORD"],
    )

    crawl_id = f"smoke:{uuid.uuid4()}"
    observed_at = iso_now()

    # Start crawl marker
    store.begin_crawl(crawl_id, mode="smoke", cutoff_iso=observed_at)

    # 1) Fetch 10 newest posts
    resp = client.list_posts(sort="new", limit=10, offset=0)
    posts = resp.get("posts", []) or []
    if not posts:
        raise RuntimeError("No posts returned from /posts. Check API key/base URL.")

    # 2) Normalize submolt dict -> name for ingestion (since your API returns submolt as object)
    def norm_post(p):
        p = dict(p)
        sub = p.get("submolt")
        if isinstance(sub, dict):
            p["submolt"] = sub.get("name")
        return p

    posts_norm = [norm_post(p) for p in posts]

    # 3) Write posts + relationships (authors/submolts)
    store.upsert_posts(posts_norm, observed_at)

    # 4) Pick one post and ingest its comments (if any)
    target = pick_post_with_comments(posts)
    if target:
        pid = target["id"]
        try:
            comments = client.get_comments(pid, sort="new", limit=50)  # returns list
        except Exception:
            comments = []
        if comments:
            store.upsert_comments(pid, comments, observed_at)

    store.end_crawl(crawl_id)
    store.close()

    print("✅ Smoke test crawl_id:", crawl_id)
    print("✅ Sample post id:", posts[0]["id"])
    if target:
        print("✅ Comment test post id:", target["id"], "comment_count:", target.get("comment_count"))

if __name__ == "__main__":
    main()
