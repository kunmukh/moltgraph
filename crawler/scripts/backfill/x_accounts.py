#!/usr/bin/env python3
"""
Backfill XAccount ownership links by:
- reading Agent names from Neo4j
- calling Moltbook /agents/profile?name=<agent>
- extracting agent.owner.x_handle and metadata
- upserting (:XAccount)-[:HAS_OWNER_X] links into Neo4j
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from neo4j_store import Neo4jStore
from moltbook_client import MoltbookClient

import time
import requests

def get_profile_resilient(client, name: str, attempts: int = 6) -> dict:
    backoff = 5.0
    for k in range(1, attempts + 1):
        try:
            return client.get_agent_profile(name)
        except requests.exceptions.HTTPError as e:
            resp = getattr(e, "response", None)
            code = resp.status_code if resp is not None else None
            if code == 429 and k < attempts:
                ra = resp.headers.get("Retry-After") if resp is not None else None
                if ra:
                    try:
                        time.sleep(max(float(ra), 1.0))
                        continue
                    except Exception:
                        pass
                time.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
                continue
            raise
        except requests.exceptions.RequestException:
            if k < attempts:
                time.sleep(min(backoff, 60.0))
                backoff = min(backoff * 2, 60.0)
                continue
            raise

def iso_now() -> str:
    # Neo4j datetime() is happiest with a 'Z' suffix
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_handle(h: Any) -> Optional[str]:
    if not isinstance(h, str):
        return None
    h = h.strip()
    if not h:
        return None
    h = h.lstrip("@").strip()
    return h.lower() if h else None


def pick_owner(agent_obj: Dict[str, Any]) -> Dict[str, Any]:
    owner = agent_obj.get("owner")
    return owner if isinstance(owner, dict) else {}


def fetch_candidates(
    store: Neo4jStore,
    *,
    only_missing: bool,
    claimed_only: bool,
    limit: int,
    agent: Optional[str],
) -> List[str]:
    if agent:
        return [agent]

    if only_missing:
        q = """
        MATCH (a:Agent)
        WHERE a.name IS NOT NULL
          AND coalesce(a.is_claimed,false) = true
          AND NOT (a)-[:HAS_OWNER_X]->(:XAccount)
        RETURN a.name AS name
        ORDER BY coalesce(a.profile_last_fetched_at, datetime("1970-01-01T00:00:00Z")) ASC
        LIMIT $limit
        """
        params = {"limit": int(limit)}
    else:
        q = """
        MATCH (a:Agent)
        WHERE a.name IS NOT NULL
          AND ($claimed_only = false OR coalesce(a.is_claimed,false) = true)
        RETURN a.name AS name
        ORDER BY coalesce(a.profile_last_fetched_at, datetime("1970-01-01T00:00:00Z")) ASC
        LIMIT $limit
        """
        params = {"limit": int(limit), "claimed_only": bool(claimed_only)}

    with store.driver.session() as s:
        rows = s.run(q, **params)
        return [r["name"] for r in rows if r and r.get("name")]


def upsert_owner_link(
    store: Neo4jStore,
    *,
    agent_name: str,
    handle: str,
    observed_at: str,
    owner: Dict[str, Any],
    dry_run: bool,
) -> None:
    url = owner.get("x_url") or owner.get("xUrl") or f"https://x.com/{handle}"

    # Optional metadata (safe even if missing)
    x_name = owner.get("x_name") or owner.get("xName")
    x_avatar = owner.get("x_avatar") or owner.get("xAvatar")
    x_bio = owner.get("x_bio") or owner.get("xBio")
    x_follower_count = owner.get("x_follower_count") if "x_follower_count" in owner else owner.get("xFollowerCount")
    x_following_count = owner.get("x_following_count") if "x_following_count" in owner else owner.get("xFollowingCount")
    x_verified = owner.get("x_verified") if "x_verified" in owner else owner.get("xVerified")

    if dry_run:
        print(f"[DRY] link Agent({agent_name}) -> XAccount({handle}) url={url}")
        return

    q = """
    MATCH (a:Agent {name:$agent})
    MERGE (x:XAccount {handle:$handle})
      ON CREATE SET x.first_seen_at = datetime($obs)
    SET x.last_seen_at = datetime($obs),
        x.url = coalesce($url, x.url),
        x.name = coalesce($x_name, x.name),
        x.avatar_url = coalesce($x_avatar, x.avatar_url),
        x.bio = coalesce($x_bio, x.bio),
        x.follower_count = coalesce($x_follower_count, x.follower_count),
        x.following_count = coalesce($x_following_count, x.following_count),
        x.is_verified = coalesce($x_verified, x.is_verified)

    MERGE (a)-[r:HAS_OWNER_X]->(x)
      ON CREATE SET r.first_seen_at = datetime($obs)
    SET r.last_seen_at = datetime($obs),

        // Helpful: also stamp agent fields if empty
        a.owner_twitter_handle = coalesce(a.owner_twitter_handle, $handle)
    """
    with store.driver.session() as s:
        s.run(
            q,
            agent=agent_name,
            handle=handle,
            url=url,
            obs=observed_at,
            x_name=x_name,
            x_avatar=x_avatar,
            x_bio=x_bio,
            x_follower_count=x_follower_count,
            x_following_count=x_following_count,
            x_verified=x_verified,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI"), required=False)
    ap.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER"), required=False)
    ap.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD"), required=False)

    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--only-missing", action="store_true", help="Only agents missing HAS_OWNER_X (default)")
    ap.add_argument("--all", action="store_true", help="Process agents even if they already have HAS_OWNER_X")
    ap.add_argument("--claimed-only", action="store_true", help="Only claimed agents (default)")
    ap.add_argument("--include-unclaimed", action="store_true", help="Include unclaimed agents too (not recommended)")
    ap.add_argument("--agent", type=str, default=None, help="Process a single agent by name")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-every", type=int, default=100)

    args = ap.parse_args()

    if not args.neo4j_uri or not args.neo4j_user or not args.neo4j_password:
        print("Missing Neo4j connection env vars. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.", file=sys.stderr)
        return 2

    only_missing = True
    if args.all:
        only_missing = False
    elif args.only_missing:
        only_missing = True

    claimed_only = True
    if args.include_unclaimed:
        claimed_only = False
    elif args.claimed_only:
        claimed_only = True

    store = Neo4jStore(args.neo4j_uri, args.neo4j_user, args.neo4j_password)
    client = MoltbookClient()

    observed_at = iso_now()

    names = fetch_candidates(
        store,
        only_missing=only_missing,
        claimed_only=claimed_only,
        limit=args.limit,
        agent=args.agent,
    )

    print(f"[backfill] candidates={len(names)} only_missing={only_missing} claimed_only={claimed_only} dry_run={args.dry_run}")

    linked = 0
    skipped_no_owner = 0
    skipped_no_handle = 0
    errors = 0

    try:
        for i, name in enumerate(names, 1):
            try:
                prof = get_profile_resilient(client, name)
                agent_obj = prof.get("agent") or {}
                if not isinstance(agent_obj, dict) or not agent_obj:
                    skipped_no_owner += 1
                    continue

                owner = pick_owner(agent_obj)
                if not owner:
                    skipped_no_owner += 1
                    continue

                handle = clean_handle(owner.get("x_handle") or owner.get("xHandle"))
                if not handle:
                    skipped_no_handle += 1
                    continue

                upsert_owner_link(
                    store,
                    agent_name=name,
                    handle=handle,
                    observed_at=observed_at,
                    owner=owner,
                    dry_run=args.dry_run,
                )
                linked += 1

            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                print(f"[error] agent={name}: {e}", file=sys.stderr)

            if args.print_every > 0 and i % args.print_every == 0:
                print(f"[backfill] {i}/{len(names)} linked={linked} no_owner={skipped_no_owner} no_handle={skipped_no_handle} errors={errors}")

    finally:
        store.close()

    print(f"[done] linked={linked} no_owner={skipped_no_owner} no_handle={skipped_no_handle} errors={errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())