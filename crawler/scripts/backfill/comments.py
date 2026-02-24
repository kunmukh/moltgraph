#!/usr/bin/env python3
"""
Backfill missing comments for posts already in Neo4j by:
  1) querying Neo4j for posts where stored_comments < p.comment_count
  2) fetching comments from Moltbook API for those post_ids
  3) upserting comments + edges into Neo4j via Neo4jStore.upsert_comments()

Best used after you patch upsert_comments() to handle author as dict OR string.
"""

import os
import time
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from moltbook_client import MoltbookClient
from neo4j_store import Neo4jStore


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_comment_tree(tree: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Make comment objects tolerant to camelCase vs snake_case drift.
    Recurses over `replies`.
    """
    def norm_one(c: Dict[str, Any]) -> Dict[str, Any]:
        c2 = dict(c)

        # common key drift
        if "created_at" not in c2 and "createdAt" in c2:
            c2["created_at"] = c2.get("createdAt")
        if "updated_at" not in c2 and "updatedAt" in c2:
            c2["updated_at"] = c2.get("updatedAt")
        if "reply_count" not in c2 and "replyCount" in c2:
            c2["reply_count"] = c2.get("replyCount")

        # authorName drift (if present)
        if "author_name" not in c2 and "authorName" in c2:
            c2["author_name"] = c2.get("authorName")

        # recurse
        replies = c2.get("replies")
        if isinstance(replies, list):
            c2["replies"] = [norm_one(r) for r in replies if isinstance(r, dict)]
        return c2

    return [norm_one(x) for x in tree if isinstance(x, dict)]


def _req_noauth_then_auth(client: MoltbookClient, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """
    Prefer no-auth first (public endpoints often behave better), then fallback to auth on 401/403.
    """
    try:
        return client._req(method, path, params=params, no_auth=True)  # type: ignore[attr-defined]
    except requests.exceptions.HTTPError as e:
        resp = e.response
        code = resp.status_code if resp is not None else None
        if code in (401, 403):
            return client._req(method, path, params=params, no_auth=False)  # type: ignore[attr-defined]
        raise


def fetch_comments_any(client: MoltbookClient, post_id: str, *, sort: str, limit: int) -> List[Dict[str, Any]]:
    params = {"sort": sort, "limit": int(limit), "shuffle": int(time.time() * 1000)}
    resp = _req_noauth_then_auth(client, "GET", f"/posts/{post_id}/comments", params=params)

    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        v = resp.get("comments") or resp.get("data")
        return v if isinstance(v, list) else []
    return []


def fetch_post_details_any(client: MoltbookClient, post_id: str) -> Dict[str, Any]:
    resp = _req_noauth_then_auth(client, "GET", f"/posts/{post_id}", params={"shuffle": int(time.time() * 1000)})
    if isinstance(resp, dict) and isinstance(resp.get("post"), dict):
        return resp["post"]
    return resp if isinstance(resp, dict) else {}


def get_candidate_posts(store: Neo4jStore, *, limit_posts: int, min_missing: int) -> List[Tuple[str, int, int]]:
    """
    Returns list of (post_id, expected, got) where got < expected.
    """
    q = """
    MATCH (p:Post)
    WITH p, coalesce(p.comment_count, 0) AS expected
    WHERE expected > 0
    OPTIONAL MATCH (p)<-[:ON_POST]-(c:Comment)
    WITH p, expected, count(c) AS got
    WHERE (expected - got) >= $min_missing
    RETURN p.id AS id, expected, got
    ORDER BY (expected - got) DESC, coalesce(p.last_seen_at, p.created_at) DESC
    LIMIT $limit
    """
    with store.driver.session() as s:
        res = s.run(q, limit=int(limit_posts), min_missing=int(min_missing))
        out = []
        for r in res:
            if r and r.get("id"):
                out.append((r["id"], int(r["expected"]), int(r["got"])))
        return out


def mark_post_backfill(store: Neo4jStore, post_id: str, *, status: str, expected: int, got_before: int, got_fetched: int, obs: str) -> None:
    q = """
    MATCH (p:Post {id:$id})
    SET p.comments_backfilled_at = datetime($obs),
        p.comments_backfill_status = $status,
        p.comments_backfill_expected = $expected,
        p.comments_backfill_got_before = $got_before,
        p.comments_backfill_fetched = $got_fetched
    """
    with store.driver.session() as s:
        s.run(q, id=post_id, obs=obs, status=status, expected=expected, got_before=got_before, got_fetched=got_fetched)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-posts", type=int, default=500, help="How many posts to backfill per run")
    ap.add_argument("--min-missing", type=int, default=1, help="Only backfill when (expected - got) >= this")
    ap.add_argument("--sort", type=str, default="new", help="comments sort: new/top/controversial (if supported)")
    ap.add_argument("--max-comments", type=int, default=500, help="API comments limit per post (server may cap)")
    ap.add_argument("--prefer-post-details", action="store_true",
                    help="Try /posts/:id and use post.comments if present (often a fuller tree)")
    ap.add_argument("--mark", action="store_true", help="Write backfill status fields onto Post")
    ap.add_argument("--sleep-seconds", type=float, default=0.0, help="Extra sleep between posts (in addition to client rpm)")
    args = ap.parse_args()

    # Neo4j env required
    for k in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        if not os.getenv(k):
            raise SystemExit(f"Missing env var: {k}")

    client = MoltbookClient()
    store = Neo4jStore(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])

    obs = iso_now()
    cands = get_candidate_posts(store, limit_posts=args.limit_posts, min_missing=args.min_missing)
    print(f"[backfill-comments] candidates={len(cands)} limit_posts={args.limit_posts} min_missing={args.min_missing}")

    ok = 0
    skipped_empty = 0
    errors = 0

    try:
        for i, (post_id, expected, got_before) in enumerate(cands, 1):
            try:
                tree: Optional[List[Dict[str, Any]]] = None

                if args.prefer_post_details:
                    post_obj = fetch_post_details_any(client, post_id)
                    comments = post_obj.get("comments")
                    if isinstance(comments, list) and comments:
                        tree = comments

                if tree is None:
                    tree = fetch_comments_any(client, post_id, sort=args.sort, limit=args.max_comments)

                tree = _normalize_comment_tree(tree or [])

                if not tree:
                    skipped_empty += 1
                    if args.mark:
                        mark_post_backfill(store, post_id, status="empty", expected=expected, got_before=got_before, got_fetched=0, obs=obs)
                    continue

                store.upsert_comments(post_id, tree, obs)
                ok += 1

                if args.mark:
                    mark_post_backfill(store, post_id, status="ok", expected=expected, got_before=got_before, got_fetched=len(tree), obs=obs)

            except Exception as e:
                errors += 1
                print(f"[error] post={post_id}: {e}")
                if args.mark:
                    mark_post_backfill(store, post_id, status="error", expected=expected, got_before=got_before, got_fetched=0, obs=obs)

            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

            if i % 50 == 0:
                print(f"[backfill-comments] {i}/{len(cands)} ok={ok} empty={skipped_empty} errors={errors}")

    finally:
        store.close()

    print(f"[done] ok={ok} empty={skipped_empty} errors={errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())