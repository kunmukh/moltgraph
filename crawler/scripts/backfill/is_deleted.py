#!/usr/bin/env python3
"""
Backfill is_deleted + updated_at for existing Neo4j nodes only.

Entities:
- Agent      via get_agent_profile(name)
- Submolt    via get_submolt(name)
- Post       via get_post(post_id)
- Comment    via get_comments(post_id)

Behavior:
- Updates ONLY existing nodes in Neo4j
- Updates ONLY these fields:
    * is_deleted
    * updated_at
- Preserves created_at and every other property
- Does NOT add any new audit/status columns
"""

import os
import time
import argparse
from typing import Any, Dict, List, Optional

import requests

from neo4j_store import Neo4jStore
from moltbook_client import MoltbookClient


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


def get_agent_names(store: Neo4jStore, limit_n: int) -> List[str]:
    q = """
    MATCH (a:Agent)
    WHERE a.name IS NOT NULL
    RETURN a.name AS name
    ORDER BY coalesce(a.last_seen_at, a.created_at) DESC
    LIMIT $limit
    """
    with store.driver.session() as s:
        return [r["name"] for r in s.run(q, limit=int(limit_n)) if r and r.get("name")]


def get_submolt_names(store: Neo4jStore, limit_n: int) -> List[str]:
    q = """
    MATCH (s:Submolt)
    WHERE s.name IS NOT NULL
    RETURN s.name AS name
    ORDER BY coalesce(s.last_seen_at, s.created_at) DESC
    LIMIT $limit
    """
    with store.driver.session() as s:
        return [r["name"] for r in s.run(q, limit=int(limit_n)) if r and r.get("name")]


def get_post_ids(store: Neo4jStore, limit_n: int) -> List[str]:
    q = """
    MATCH (p:Post)
    WHERE p.id IS NOT NULL
    RETURN p.id AS id
    ORDER BY coalesce(p.last_seen_at, p.created_at) DESC
    LIMIT $limit
    """
    with store.driver.session() as s:
        return [r["id"] for r in s.run(q, limit=int(limit_n)) if r and r.get("id")]


def fetch_comments_public_then_auth(
    client: MoltbookClient,
    post_id: str,
    sort: str,
    limit: int,
) -> List[Dict[str, Any]]:
    try:
        return client.get_comments(post_id, sort=sort, limit=limit, no_auth=True) or []
    except requests.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        if code in (401, 403):
            return client.get_comments(post_id, sort=sort, limit=limit, no_auth=False) or []
        raise


def update_agent(store: Neo4jStore, name: str, is_deleted: Any, updated_at: Optional[str]) -> int:
    q = """
    MATCH (a:Agent {name:$name})
    SET a.is_deleted = $is_deleted,
        a.updated_at = CASE
            WHEN $updated_at IS NULL THEN a.updated_at
            ELSE datetime($updated_at)
        END
    RETURN count(a) AS updated
    """
    with store.driver.session() as s:
        rec = s.run(q, name=name, is_deleted=is_deleted, updated_at=updated_at).single()
        return int(rec["updated"]) if rec and rec.get("updated") is not None else 0


def update_submolt(store: Neo4jStore, name: str, is_deleted: Any, updated_at: Optional[str]) -> int:
    q = """
    MATCH (s:Submolt {name:$name})
    SET s.is_deleted = $is_deleted,
        s.updated_at = CASE
            WHEN $updated_at IS NULL THEN s.updated_at
            ELSE datetime($updated_at)
        END
    RETURN count(s) AS updated
    """
    with store.driver.session() as s:
        rec = s.run(q, name=name, is_deleted=is_deleted, updated_at=updated_at).single()
        return int(rec["updated"]) if rec and rec.get("updated") is not None else 0


def update_post(store: Neo4jStore, post_id: str, is_deleted: Any, updated_at: Optional[str]) -> int:
    q = """
    MATCH (p:Post {id:$id})
    SET p.is_deleted = $is_deleted,
        p.updated_at = CASE
            WHEN $updated_at IS NULL THEN p.updated_at
            ELSE datetime($updated_at)
        END
    RETURN count(p) AS updated
    """
    with store.driver.session() as s:
        rec = s.run(q, id=post_id, is_deleted=is_deleted, updated_at=updated_at).single()
        return int(rec["updated"]) if rec and rec.get("updated") is not None else 0


def update_comments_batch(store: Neo4jStore, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    q = """
    UNWIND $rows AS row
    MATCH (c:Comment {id: row.id})
    SET c.is_deleted = row.is_deleted,
        c.updated_at = CASE
            WHEN row.updated_at IS NULL THEN c.updated_at
            ELSE datetime(row.updated_at)
        END
    RETURN count(c) AS updated
    """
    with store.driver.session() as s:
        rec = s.run(q, rows=rows).single()
        return int(rec["updated"]) if rec and rec.get("updated") is not None else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-agents", type=int, default=5000)
    ap.add_argument("--limit-submolts", type=int, default=2000)
    ap.add_argument("--limit-posts", type=int, default=10000)
    ap.add_argument("--comments-sort", type=str, default="new")
    ap.add_argument("--max-comments", type=int, default=500)
    ap.add_argument("--skip-agents", action="store_true")
    ap.add_argument("--skip-submolts", action="store_true")
    ap.add_argument("--skip-posts", action="store_true")
    ap.add_argument("--skip-comments", action="store_true")
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

    agent_updates = 0
    submolt_updates = 0
    post_updates = 0
    comment_updates = 0
    no_delete_field = 0
    errors = 0

    try:
        if not args.skip_agents:
            names = get_agent_names(store, args.limit_agents)
            print(f"[agents] checking {len(names)}")
            for i, name in enumerate(names, 1):
                try:
                    prof = client.get_agent_profile(name) or {}
                    agent = prof.get("agent") if isinstance(prof.get("agent"), dict) else prof
                    if isinstance(agent, dict) and "is_deleted" in agent:
                        agent_updates += update_agent(
                            store,
                            name,
                            agent.get("is_deleted"),
                            agent.get("updated_at") or agent.get("updatedAt"),
                        )
                    else:
                        no_delete_field += 1
                except Exception as e:
                    errors += 1
                    print(f"[error][agent] {name}: {e}")

                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
                if i % 100 == 0:
                    print(f"[agents] {i}/{len(names)} updated={agent_updates} no_field={no_delete_field} errors={errors}")

        if not args.skip_submolts:
            names = get_submolt_names(store, args.limit_submolts)
            print(f"[submolts] checking {len(names)}")
            for i, name in enumerate(names, 1):
                try:
                    sub = client.get_submolt(name) or {}
                    if isinstance(sub, dict) and "is_deleted" in sub:
                        submolt_updates += update_submolt(
                            store,
                            name,
                            sub.get("is_deleted"),
                            sub.get("updated_at") or sub.get("updatedAt"),
                        )
                    else:
                        no_delete_field += 1
                except Exception as e:
                    errors += 1
                    print(f"[error][submolt] {name}: {e}")

                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
                if i % 100 == 0:
                    print(f"[submolts] {i}/{len(names)} updated={submolt_updates} no_field={no_delete_field} errors={errors}")

        post_ids: List[str] = []
        if not args.skip_posts or not args.skip_comments:
            post_ids = get_post_ids(store, args.limit_posts)

        if not args.skip_posts:
            print(f"[posts] checking {len(post_ids)}")
            for i, post_id in enumerate(post_ids, 1):
                try:
                    post = client.get_post(post_id) or {}
                    if isinstance(post, dict) and "is_deleted" in post:
                        post_updates += update_post(
                            store,
                            post_id,
                            post.get("is_deleted"),
                            post.get("updated_at") or post.get("updatedAt"),
                        )
                    else:
                        no_delete_field += 1
                except Exception as e:
                    errors += 1
                    print(f"[error][post] {post_id}: {e}")

                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
                if i % 100 == 0:
                    print(f"[posts] {i}/{len(post_ids)} updated={post_updates} no_field={no_delete_field} errors={errors}")

        if not args.skip_comments:
            print(f"[comments] checking comments for {len(post_ids)} posts")
            for i, post_id in enumerate(post_ids, 1):
                try:
                    tree = fetch_comments_public_then_auth(
                        client,
                        post_id,
                        sort=args.comments_sort,
                        limit=args.max_comments,
                    )
                    flat = flatten_comments(tree)

                    rows = []
                    for c in flat:
                        cid = c.get("id")
                        if not cid:
                            continue
                        if "is_deleted" not in c:
                            continue
                        rows.append(
                            {
                                "id": cid,
                                "is_deleted": c.get("is_deleted"),
                                "updated_at": c.get("updated_at") or c.get("updatedAt"),
                            }
                        )

                    comment_updates += update_comments_batch(store, rows)
                except Exception as e:
                    errors += 1
                    print(f"[error][comments] post={post_id}: {e}")

                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
                if i % 100 == 0:
                    print(f"[comments] {i}/{len(post_ids)} updated={comment_updates} errors={errors}")

    finally:
        store.close()

    print(
        "[done] "
        f"agents_updated={agent_updates} "
        f"submolts_updated={submolt_updates} "
        f"posts_updated={post_updates} "
        f"comments_updated={comment_updates} "
        f"no_delete_field={no_delete_field} "
        f"errors={errors}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())