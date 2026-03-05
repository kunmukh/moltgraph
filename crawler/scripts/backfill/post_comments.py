# crawler/scripts/backfill_post_comments.py
import os
import argparse
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List

from moltbook_client import MoltbookClient
from neo4j_store import Neo4jStore

def iso_now() -> str:
    # Neo4j datetime() is happiest with a 'Z' suffix
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def mark_post_deleted_404(store: Neo4jStore, post_id: str, obs: str, *, reason: str = "api_404") -> None:
    """404 from /posts/:id => post no longer exists (mark deleted but keep node)."""
    q = """
    MERGE (p:Post {id:$id})
    ON CREATE SET p.first_seen_at = datetime($obs),
                  p.created_at = datetime($obs)
    SET p.last_seen_at = datetime($obs),
        p.updated_at = datetime($obs),
        p.is_deleted = true,
        p.deleted_at = datetime($obs),
        p.deletion_reason = $reason
    """
    with store.driver.session() as s:
        s.run(q, id=post_id, obs=obs, reason=reason)


def mark_comments_deleted_for_post(store: Neo4jStore, post_id: str, obs: str, *, reason: str = "api_404") -> None:
    """If a post is deleted, any comments we previously stored under it should be marked deleted too."""
    q = """
    MATCH (p:Post {id:$id})
    OPTIONAL MATCH (c:Comment)-[:ON_POST]->(p)
    WITH collect(c) AS cs
    FOREACH (c IN cs |
        SET c.last_seen_at = datetime($obs),
            c.updated_at = datetime($obs),
            c.is_deleted = true,
            c.deleted_at = datetime($obs),
            c.deletion_reason = $reason
    )
    """
    with store.driver.session() as s:
        s.run(q, id=post_id, obs=obs, reason=reason)

def fetch_comments_fallback(client: MoltbookClient, post_id: str, sort: str, limit: int) -> List[Dict[str, Any]]:
    # try public (no auth) first
    try:
        return client.get_comments(post_id, sort=sort, limit=limit, shuffle=False, no_auth=True)
    except requests.HTTPError as e:
        # if public fails, try authed
        code = getattr(e.response, "status_code", None)
        if code not in (401, 403):
            raise

    # auth attempt (works only if MOLTBOOK_API_KEY is set)
    return client.get_comments(post_id, sort=sort, limit=limit, shuffle=False, no_auth=False)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-id", required=True)
    ap.add_argument("--sort", default="new")
    ap.add_argument("--limit", type=int, default=int(os.getenv("COMMENTS_LIMIT_PER_POST", "200")))
    args = ap.parse_args()

    client = MoltbookClient()
    store = Neo4jStore(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
    obs = iso_now()

    # 1) Fetch post details and upsert the post (so the Comment->Post rel can be created)
    post_obj: Dict[str, Any] = {}
    try:
        post_obj = client.get_post(args.post_id) or {}
    except requests.exceptions.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code == 404:
            mark_post_deleted_404(store, args.post_id, obs)
            mark_comments_deleted_for_post(store, args.post_id, obs)
            print(f"[deleted][post] {args.post_id}: inferred via 404")
            store.close()
            return
        print(f"[backfill][WARN] get_post failed for {args.post_id}: {e}")
    except Exception as e:
        print(f"[backfill][WARN] get_post failed for {args.post_id}: {e}")

    if post_obj and isinstance(post_obj, dict):
        try:
            store.upsert_posts([post_obj], obs)
            print(f"[backfill] upserted Post {args.post_id}")
        except Exception as e:
            print(f"[backfill][WARN] upsert_posts failed for {args.post_id}: {e}")

    # 2) Fetch comments and upsert
    comments: List[Dict[str, Any]] = []
    try:
        comments = fetch_comments_fallback(client, args.post_id, sort=args.sort, limit=args.limit)
    except requests.exceptions.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code == 404:
            mark_post_deleted_404(store, args.post_id, obs)
            mark_comments_deleted_for_post(store, args.post_id, obs)
            print(f"[deleted][post] {args.post_id}: inferred via 404 (comments endpoint)")
            store.close()
            return
        print(f"[backfill][ERROR] fetching comments failed for {args.post_id}: {e}")
        store.close()
        raise
    except Exception as e:
        print(f"[backfill][ERROR] fetching comments failed for {args.post_id}: {e}")
        store.close()
        raise

    print(f"[backfill] fetched comments={len(comments)} for post={args.post_id}")
    if comments:
        store.upsert_comments(args.post_id, comments, obs)
        print(f"[backfill] upserted comments={len(comments)} for post={args.post_id}")

    store.close()

if __name__ == "__main__":
    main()