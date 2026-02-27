#!/usr/bin/env python3
"""
Backfill moderation fields for existing Post / Comment nodes in Neo4j.

Updates:
- Post.is_spam
- Post.verification_status
- Comment.is_spam
- Comment.verification_status

Reads all Post IDs from Neo4j, fetches each post + its comments from Moltbook,
and updates existing nodes only.
"""

import os
import time
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

from neo4j_store import Neo4jStore
from moltbook_client import MoltbookClient


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def flatten_comments(tree: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []

    def rec(node: Dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        flat.append(node)
        replies = node.get("replies") or []
        if isinstance(replies, list):
            for r in replies:
                rec(r)

    for c in tree or []:
        rec(c)
    return flat


def get_post_ids(store: Neo4jStore, limit_posts: int, only_missing: bool) -> List[str]:
    if only_missing:
        q = """
        MATCH (p:Post)
        WHERE p.id IS NOT NULL
          AND (
                p.is_spam IS NULL OR
                p.verification_status IS NULL
              )
        RETURN p.id AS id
        ORDER BY coalesce(p.last_seen_at, p.created_at) DESC
        LIMIT $limit
        """
    else:
        q = """
        MATCH (p:Post)
        WHERE p.id IS NOT NULL
        RETURN p.id AS id
        ORDER BY coalesce(p.last_seen_at, p.created_at) DESC
        LIMIT $limit
        """

    with store.driver.session() as s:
        return [r["id"] for r in s.run(q, limit=int(limit_posts)) if r and r.get("id")]


def fetch_post(client: MoltbookClient, post_id: str) -> Dict[str, Any]:
    # get_post() is already public in your client
    return client.get_post(post_id) or {}


def fetch_comments(client: MoltbookClient, post_id: str, sort: str, limit: int) -> List[Dict[str, Any]]:
    # Prefer public first, then fallback to auth if needed
    try:
        return client.get_comments(post_id, sort=sort, limit=limit, no_auth=True) or []
    except requests.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        if code in (401, 403):
            return client.get_comments(post_id, sort=sort, limit=limit, no_auth=False) or []
        raise


def update_post_moderation(
    store: Neo4jStore,
    post_id: str,
    is_spam: Any,
    verification_status: Any,
    observed_at: str,
) -> None:
    q = """
    MATCH (p:Post {id: $id})
    SET p.is_spam = $is_spam,
        p.verification_status = $verification_status,
        p.moderation_backfilled_at = datetime($obs)
    """
    with store.driver.session() as s:
        s.run(
            q,
            id=post_id,
            is_spam=is_spam,
            verification_status=verification_status,
            obs=observed_at,
        )


def update_comment_moderation_batch(
    store: Neo4jStore,
    rows: List[Dict[str, Any]],
    observed_at: str,
) -> int:
    if not rows:
        return 0

    q = """
    UNWIND $rows AS row
    MATCH (c:Comment {id: row.id})
    SET c.is_spam = row.is_spam,
        c.verification_status = row.verification_status,
        c.moderation_backfilled_at = datetime($obs)
    RETURN count(c) AS updated
    """
    with store.driver.session() as s:
        rec = s.run(q, rows=rows, obs=observed_at).single()
        return int(rec["updated"]) if rec and rec.get("updated") is not None else 0


def mark_post_status(
    store: Neo4jStore,
    post_id: str,
    status: str,
    comments_seen: int,
    comments_updated: int,
    observed_at: str,
) -> None:
    q = """
    MATCH (p:Post {id:$id})
    SET p.moderation_backfilled_at = datetime($obs),
        p.moderation_backfill_status = $status,
        p.moderation_comments_seen = $comments_seen,
        p.moderation_comments_updated = $comments_updated
    """
    with store.driver.session() as s:
        s.run(
            q,
            id=post_id,
            obs=observed_at,
            status=status,
            comments_seen=int(comments_seen),
            comments_updated=int(comments_updated),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-posts", type=int, default=1000)
    ap.add_argument("--sort", type=str, default="new")
    ap.add_argument("--max-comments", type=int, default=500)
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--sleep-seconds", type=float, default=0.0)
    args = ap.parse_args()

    for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        if not os.getenv(k):
            raise SystemExit(f"Missing env var: {k}")

    client = MoltbookClient()
    store = Neo4jStore(
        os.environ["NEO4J_URI"],
        os.environ["NEO4J_USER"],
        os.environ["NEO4J_PASSWORD"],
    )

    obs = iso_now()
    post_ids = get_post_ids(store, args.limit_posts, args.only_missing)
    print(f"[moderation-backfill] posts={len(post_ids)} only_missing={args.only_missing}")

    ok = 0
    errors = 0
    total_comments_seen = 0
    total_comments_updated = 0

    try:
        for i, post_id in enumerate(post_ids, 1):
            try:
                post_obj = fetch_post(client, post_id)
                if not post_obj:
                    if args.mark:
                        mark_post_status(store, post_id, "post_empty", 0, 0, obs)
                    continue

                update_post_moderation(
                    store,
                    post_id=post_id,
                    is_spam=post_obj.get("is_spam"),
                    verification_status=post_obj.get("verification_status"),
                    observed_at=obs,
                )

                comments_tree = fetch_comments(
                    client,
                    post_id=post_id,
                    sort=args.sort,
                    limit=args.max_comments,
                )
                comments_flat = flatten_comments(comments_tree)

                rows = []
                for c in comments_flat:
                    cid = c.get("id")
                    if not cid:
                        continue
                    rows.append(
                        {
                            "id": cid,
                            "is_spam": c.get("is_spam"),
                            "verification_status": c.get("verification_status"),
                        }
                    )

                comments_seen = len(comments_flat)
                comments_updated = update_comment_moderation_batch(store, rows, obs)

                total_comments_seen += comments_seen
                total_comments_updated += comments_updated
                ok += 1

                if args.mark:
                    mark_post_status(store, post_id, "ok", comments_seen, comments_updated, obs)

            except Exception as e:
                errors += 1
                print(f"[error] post={post_id}: {e}")
                if args.mark:
                    mark_post_status(store, post_id, "error", 0, 0, obs)

            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

            if i % 50 == 0:
                print(
                    f"[moderation-backfill] {i}/{len(post_ids)} "
                    f"ok={ok} errors={errors} "
                    f"comments_seen={total_comments_seen} comments_updated={total_comments_updated}"
                )

    finally:
        store.close()

    print(
        f"[done] ok={ok} errors={errors} "
        f"comments_seen={total_comments_seen} comments_updated={total_comments_updated}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())