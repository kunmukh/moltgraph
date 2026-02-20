import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
import inspect

import requests

from moltbook_client import MoltbookClient
from neo4j_store import Neo4jStore


# --------------------------
# Helpers
# --------------------------
def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_list(resp: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
    """Return the first list found among keys (API shape drift)."""
    for k in keys:
        v = resp.get(k)
        if isinstance(v, list):
            return v
    return []


def submolt_name(sub: Any) -> Optional[str]:
    if isinstance(sub, dict):
        return sub.get("name")
    if isinstance(sub, str):
        return sub
    return None


def norm_post_for_store(p: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure p['submolt'] is a string name so Neo4j doesn't try to store maps."""
    p2 = dict(p)
    p2["submolt"] = submolt_name(p.get("submolt"))
    return p2


def extract_author_name(obj: Dict[str, Any]) -> Optional[str]:
    a = obj.get("author")
    if isinstance(a, dict):
        return a.get("name")
    if isinstance(obj.get("author_name"), str):
        return obj.get("author_name")
    return None


def collect_authors_from_comments(tree: List[Dict[str, Any]], out: Set[str]) -> None:
    for c in tree or []:
        n = extract_author_name(c)
        if n:
            out.add(n)
        replies = c.get("replies") or []
        if isinstance(replies, list) and replies:
            collect_authors_from_comments(replies, out)


def upsert_agents_profile_aware(store: Neo4jStore, agents: List[Dict[str, Any]], observed_at: str) -> None:
    """
    Works with both versions of Neo4jStore:
      - upsert_agents(agents, observed_at)
      - upsert_agents(agents, observed_at, mark_profile=True)
    """
    if not agents:
        return
    try:
        sig = inspect.signature(store.upsert_agents)
        if "mark_profile" in sig.parameters:
            store.upsert_agents(agents, observed_at, mark_profile=True)  # type: ignore[arg-type]
        else:
            store.upsert_agents(agents, observed_at)
    except TypeError:
        store.upsert_agents(agents, observed_at)


# --------------------------
# Public GET wrapper (no Authorization header)
# --------------------------
def public_get_json(client: MoltbookClient, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Moltbook's public listing endpoints (/posts, /submolts, /posts/:id/comments, etc.)
    often behave better WITHOUT Authorization (avoid personalized/cached first-page issues).

    We still reuse client's rate-limit pacing via _sleep_if_needed().
    """
    base = getattr(client, "base", os.getenv("MOLTBOOK_BASE_URL", "https://www.moltbook.com/api/v1").rstrip("/"))
    ua = getattr(client, "ua", os.getenv("USER_AGENT", "MoltGraphCrawler/0.1"))

    url = f"{base}{path}"
    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    max_tries = 8
    backoff = 1.5
    for attempt in range(1, max_tries + 1):
        # pace requests if the client has a limiter
        try:
            client._sleep_if_needed()  # type: ignore[attr-defined]
        except Exception:
            pass

        r = requests.get(url, headers=headers, params=params, timeout=60)

        # Retryable
        if r.status_code in (429, 502, 503, 504):
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-Reset")
                if reset:
                    wait = max(float(reset) - time.time(), 1.0)
                    time.sleep(wait)
                    continue
            if attempt < max_tries:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

        # Sometimes endpoints unexpectedly require auth; caller can fallback to client._req
        if r.status_code == 401:
            raise PermissionError("401 Unauthorized on public_get_json")

        r.raise_for_status()
        return r.json()

    r.raise_for_status()
    return {}  # unreachable


def get_comments_any(client: MoltbookClient, post_id: str, sort: str, limit: int) -> List[Dict[str, Any]]:
    """
    Try public first, then auth fallback.
    """
    params = {"sort": sort, "limit": limit}
    try:
        resp = public_get_json(client, f"/posts/{post_id}/comments", params=params)
    except PermissionError:
        resp = client._req("GET", f"/posts/{post_id}/comments", params=params)  # type: ignore[attr-defined]

    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        v = resp.get("comments") or resp.get("data") or resp.get("posts")  # just in case
        return v if isinstance(v, list) else []
    return []


# --------------------------
# Main
# --------------------------
def main():
    client = MoltbookClient()
    store = Neo4jStore(
        os.environ["NEO4J_URI"],
        os.environ["NEO4J_USER"],
        os.environ["NEO4J_PASSWORD"],
    )

    crawl_id = f"full:{uuid.uuid4()}"
    cutoff = iso_now()
    observed_at = iso_now()

    # --------------------------
    # Feature flags / knobs (defaults)
    # --------------------------
    fetch_post_details = os.getenv("FETCH_POST_DETAILS", "0") == "1"
    scrape_html = os.getenv("SCRAPE_AGENT_HTML", "0") == "1"

    crawl_comments = os.getenv("CRAWL_COMMENTS", "1") == "1"
    comments_limit_per_post = int(os.getenv("COMMENTS_LIMIT_PER_POST", "200"))

    # Prefer full nested comment trees from /posts/:id when we already fetch post details.
    # This avoids the hard limit on /posts/:id/comments and gets more complete trees.
    comments_from_post_details = os.getenv("COMMENTS_FROM_POST_DETAILS", "1") == "1"

    # /submolts offset is currently ignored in production in many cases; treat as "top slice" only.
    submolt_top_limit = int(os.getenv("SUBMOLT_TOP_LIMIT", "100"))
    moderators_limit = int(os.getenv("MODERATOR_SUBMOLTS_LIMIT", "500"))

    enrich_submolts = os.getenv("ENRICH_SUBMOLTS", "0") == "1"
    enrich_submolts_limit = int(os.getenv("ENRICH_SUBMOLTS_LIMIT", "0"))  # 0 = no cap

    fetch_agent_profiles = os.getenv("FETCH_AGENT_PROFILES", "1") == "1"
    profile_limit = int(os.getenv("PROFILE_LIMIT", "0"))  # 0 = no cap

    # posts paging controls
    page = int(os.getenv("POSTS_PAGE_SIZE", "50"))
    max_stale_pages = int(os.getenv("MAX_STALE_PAGES", "4"))  # stop if offset ignored (no new IDs)
    max_repeat_pages = int(os.getenv("MAX_REPEAT_PAGES", "2"))  # stop if same signature repeats
    max_pages = int(os.getenv("POSTS_MAX_PAGES", "0"))  # 0 = no cap

    # Optional additional "views" (sort,time) to widen coverage even if paging is flaky.
    # (Undocumented but used in other crawlers; safe if ignored by API.)
    views_env = os.getenv("POST_VIEWS", "").strip()
    if views_env:
        # format: "new:|top:day|top:week|hot:day"
        views = []
        for part in views_env.split("|"):
            if ":" in part:
                s, t = part.split(":", 1)
                views.append((s.strip(), t.strip() or None))
            else:
                views.append((part.strip(), None))
    else:
        views = [
            ("new", None),
            ("top", "day"),
            ("top", "week"),
            ("top", "month"),
            ("top", "year"),
            ("top", "all"),
            ("hot", "day"),
            ("hot", "week"),
        ]

    store.begin_crawl(crawl_id, mode="full", cutoff_iso=cutoff)

    # 1) Save "me" (auth)
    try:
        me = client.get_me()
        store.upsert_agents([me], observed_at)
    except Exception:
        pass

    # 2) Submolts seed slice (public)
    submolts_seed: List[Dict[str, Any]] = []
    if submolt_top_limit > 0:
        try:
            resp = public_get_json(client, "/submolts", params={"sort": "popular", "limit": submolt_top_limit, "offset": 0})
            submolts_seed = _as_list(resp, "submolts", "data")
            if submolts_seed:
                store.upsert_submolts(submolts_seed, observed_at)
                print(f"[submolts] wrote top slice: {len(submolts_seed)} (pagination may be ignored)")

            if enrich_submolts and submolts_seed:
                enriched: List[Dict[str, Any]] = []
                for i, s in enumerate(submolts_seed, 1):
                    name = s.get("name")
                    if not name:
                        continue
                    if enrich_submolts_limit and len(enriched) >= enrich_submolts_limit:
                        break
                    try:
                        det = public_get_json(client, f"/submolts/{name}")
                        # shape can be {"submolt": {...}} or directly {...}
                        enriched.append(det.get("submolt") if isinstance(det, dict) else s)  # type: ignore[union-attr]
                    except Exception:
                        enriched.append(s)
                    if i % 50 == 0:
                        print(f"[submolts] enriched {i}/{len(submolts_seed)}")
                if enriched:
                    store.upsert_submolts(enriched, observed_at)
        except Exception as e:
            print(f"[submolts] seed slice failed: {e}")

    # 3) Crawl posts (public) — multi-view scan + robust stop conditions
    seen_post_ids: Set[str] = set()
    commented_post_ids: Set[str] = set()
    # In-batch cache: post_id -> nested comment tree from /posts/:id
    post_comments_cache: Dict[str, List[Dict[str, Any]]] = {}
    seen_agents: Set[str] = set()
    submolts_seen: Dict[str, Dict[str, Any]] = {}
    comments_posts_with_tree = 0

    written_total = 0

    for (sort, time_window) in views:
        offset = store.get_checkpoint(crawl_id, f"posts_offset_{sort}_{time_window or 'na'}")
        stale_pages = 0
        repeat_pages = 0
        prev_sig = None
        pages = 0

        print(f"[posts] view sort={sort} time={time_window or '-'} starting offset={offset} page={page}")

        while True:
            params: Dict[str, Any] = {"sort": sort, "limit": page, "offset": offset}
            if time_window:
                params["time"] = time_window
            # cache-buster: helps when CDN ignores offset
            params["shuffle"] = int(time.time() * 1000)

            try:
                resp = public_get_json(client, "/posts", params=params)
            except PermissionError:
                # fallback to auth if public blocked
                resp = client.list_posts(sort=sort, limit=page, offset=offset)

            batch = _as_list(resp, "posts", "data")
            if not batch:
                print("[posts] empty batch; stopping view")
                break

            # signature of first 10 IDs to detect "same page again"
            sig = tuple(p.get("id") for p in batch[:10])
            if sig == prev_sig:
                repeat_pages += 1
            else:
                repeat_pages = 0
            prev_sig = sig

            # Optional: enrich new posts only
            new_batch: List[Dict[str, Any]] = []
            new_ids = 0

            for p in batch:
                pid = p.get("id")
                if not pid:
                    continue
                if pid not in seen_post_ids:
                    seen_post_ids.add(pid)
                    new_ids += 1

                # submolt discovery
                sub = p.get("submolt")
                nm = submolt_name(sub)
                if nm:
                    if isinstance(sub, dict):
                        # keep richest dict we have seen for that name
                        submolts_seen[nm] = {**submolts_seen.get(nm, {"name": nm}), **sub}
                    else:
                        submolts_seen.setdefault(nm, {"name": nm})

                # author discovery
                an = extract_author_name(p)
                if an:
                    seen_agents.add(an)

            if fetch_post_details:
                # enrich only posts we haven't seen before in this run (new_ids gate above isn't per-post),
                # so we re-check within the loop
                for p in batch:
                    pid = p.get("id")
                    if not pid:
                        continue
                    # only enrich when we first see the post
                    # (we don't store a separate "enriched" set; this is good enough)
                    try:
                        det = public_get_json(client, f"/posts/{pid}", params={"shuffle": int(time.time() * 1000)})
                        post_obj = det.get("post") if isinstance(det, dict) else None
                        new_batch.append(post_obj or p)

                        # Cache full comment tree from post details if present
                        if crawl_comments and comments_from_post_details and pid not in commented_post_ids:
                            if isinstance(post_obj, dict):
                                tree = post_obj.get("comments")
                                if isinstance(tree, list) and tree:
                                    post_comments_cache[pid] = tree
                    except Exception:
                        new_batch.append(p)
            else:
                new_batch = batch

            # Write posts
            store.upsert_posts([norm_post_for_store(p) for p in new_batch], observed_at)
            written_total += len(batch)            # Comments: fetch at most once per post ID
            if crawl_comments:
                for p in batch:
                    pid = p.get("id")
                    if not pid or pid in commented_post_ids:
                        continue

                    # 1) Prefer cached full tree from /posts/:id (when FETCH_POST_DETAILS=1)
                    tree = post_comments_cache.pop(pid, None)
                    if not tree:
                        # 2) Fallback: /posts/:id/comments (NOTE: server-side hard limit; may be incomplete)
                        try:
                            tree = get_comments_any(client, pid, sort="new", limit=comments_limit_per_post)
                        except Exception:
                            tree = None

                    commented_post_ids.add(pid)
                    if tree:
                        try:
                            store.upsert_comments(pid, tree, observed_at)
                            collect_authors_from_comments(tree, seen_agents)
                            comments_posts_with_tree += 1
                        except Exception:
                            pass

            # offset advance
            old_offset = offset
            nxt = resp.get("next_offset")
            try:
                nxt_i = int(nxt)
            except Exception:
                nxt_i = None

            if nxt_i is not None and nxt_i > offset:
                offset = nxt_i
            else:
                offset += len(batch)

            store.set_checkpoint(crawl_id, f"posts_offset_{sort}_{time_window or 'na'}", offset)

            first_id = batch[0].get("id")
            last_id = batch[-1].get("id")
            print(
                f"[posts] wrote_total={written_total} batch={len(batch)} new_ids={new_ids} "
                f"has_more={resp.get('has_more')} offset:{old_offset}->{offset} "
                f"first={first_id} last={last_id} "
                f"submolts_seen={len(submolts_seen)} agents_seen={len(seen_agents)} comments_posts_with_tree={comments_posts_with_tree}"
            )

            # stop conditions
            if new_ids == 0:
                stale_pages += 1
            else:
                stale_pages = 0

            pages += 1
            if max_pages and pages >= max_pages:
                print(f"[posts] reached POSTS_MAX_PAGES={max_pages}; stopping view")
                break

            if repeat_pages >= max_repeat_pages:
                print("[posts] WARNING: same page signature repeating; stopping view to avoid infinite loop.")
                break

            if stale_pages >= max_stale_pages:
                print("[posts] WARNING: no new post IDs for several pages (offset likely ignored); stopping view.")
                break

            if not resp.get("has_more"):
                break

    # 4) Upsert submolts discovered from posts (this is the main scaler)
    discovered_submolts = list(submolts_seen.values())
    if discovered_submolts:
        store.upsert_submolts(discovered_submolts, observed_at)
        print(f"[submolts] upserted discovered from posts: {len(discovered_submolts)}")

        # 4b) Optional: crawl per-submolt feeds to widen coverage (posts that may not surface in global views)
        crawl_submolt_feeds = os.getenv("CRAWL_SUBMOLT_FEEDS", "0") == "1"
        submolt_feed_max_pages = int(os.getenv("SUBMOLT_FEED_MAX_PAGES", "0"))  # 0 = skip
        submolt_feed_sort = os.getenv("SUBMOLT_FEED_SORT", "new").strip() or "new"
        submolt_feed_limit = int(os.getenv("SUBMOLT_FEED_LIMIT", "0"))  # 0 = no cap on number of submolts

        if crawl_submolt_feeds and submolt_feed_max_pages and discovered_submolts:
            names = [s.get("name") for s in discovered_submolts if s.get("name")]
            if submolt_feed_limit and len(names) > submolt_feed_limit:
                names = names[:submolt_feed_limit]
            print(f"[submolt-feed] crawling feeds for {len(names)} submolts (pages={submolt_feed_max_pages}, sort={submolt_feed_sort})")

            for sm in names:
                key = f"submolt_feed_offset_{sm}"
                offset = store.get_checkpoint(crawl_id, key)
                pages = 0
                prev_sig = None
                repeat_pages = 0
                stale_pages = 0

                while True:
                    params = {"sort": submolt_feed_sort, "limit": page, "offset": offset, "shuffle": int(time.time() * 1000)}
                    try:
                        resp = public_get_json(client, f"/submolts/{sm}/feed", params=params)
                    except Exception:
                        break

                    batch = _as_list(resp, "posts", "data")
                    if not batch:
                        break

                    sig = tuple(p.get("id") for p in batch[:10])
                    if sig == prev_sig:
                        repeat_pages += 1
                    else:
                        repeat_pages = 0
                    prev_sig = sig

                    new_ids = 0
                    for p in batch:
                        pid = p.get("id")
                        if pid and pid not in seen_post_ids:
                            seen_post_ids.add(pid)
                            new_ids += 1
                        sub = p.get("submolt")
                        nm = submolt_name(sub)
                        if nm and isinstance(sub, dict):
                            submolts_seen[nm] = {**submolts_seen.get(nm, {"name": nm}), **sub}
                        an = extract_author_name(p)
                        if an:
                            seen_agents.add(an)

                    store.upsert_posts([norm_post_for_store(p) for p in batch], observed_at)

                    old_offset = offset
                    nxt = resp.get("next_offset")
                    try:
                        nxt_i = int(nxt)
                    except Exception:
                        nxt_i = None

                    if nxt_i is not None and nxt_i > offset:
                        offset = nxt_i
                    else:
                        offset += len(batch)

                    store.set_checkpoint(crawl_id, key, offset)

                    print(f"[submolt-feed] {sm} batch={len(batch)} new_ids={new_ids} offset:{old_offset}->{offset} has_more={resp.get('has_more')}")

                    if new_ids == 0:
                        stale_pages += 1
                    else:
                        stale_pages = 0

                    pages += 1
                    if pages >= submolt_feed_max_pages:
                        break
                    if repeat_pages >= max_repeat_pages:
                        break
                    if stale_pages >= max_stale_pages:
                        break
                    if not resp.get("has_more"):
                        break

    # 5) Moderators for discovered submolts (cap calls)
    if moderators_limit > 0 and discovered_submolts:
        to_mod = discovered_submolts[:moderators_limit]
        print(f"[mods] refreshing moderators for {len(to_mod)} submolts (limit={moderators_limit})")
        for i, s in enumerate(to_mod, 1):
            name = s.get("name")
            if not name:
                continue
            try:
                try:
                    resp = public_get_json(client, f"/submolts/{name}/moderators", params={"shuffle": int(time.time() * 1000)})
                    mods = resp.get("moderators", []) if isinstance(resp, dict) else []
                except PermissionError:
                    mods = client.get_moderators(name)

                if isinstance(mods, list) and mods:
                    store.upsert_moderators_for_submolt(name, mods, observed_at)
                    # Moderator payloads can be wrapper objects like {"role": "...", "agent": {<profile>}}.
                    # Extract agent dicts / names so upserts don't silently drop them.
                    mod_agents: List[Dict[str, Any]] = []
                    for mm in mods:
                        if not isinstance(mm, dict):
                            continue
                        af = mm.get("agent")
                        if isinstance(af, dict):
                            mod_agents.append(af)
                        elif isinstance(af, str) and af:
                            mod_agents.append({"name": af})
                        elif isinstance(mm.get("name"), str) and mm.get("name"):
                            mod_agents.append(mm)
                        elif isinstance(mm.get("agent_name"), str) and mm.get("agent_name"):
                            mod_agents.append({"name": mm.get("agent_name")})
                    upsert_agents_profile_aware(store, mod_agents, observed_at)
                    for m in mods:
                        nm = m.get("name") or m.get("agent_name")
                        if not nm:
                            af = m.get("agent")
                            if isinstance(af, str):
                                nm = af
                            elif isinstance(af, dict):
                                nm = af.get("name")
                        if isinstance(nm, str) and nm:
                            seen_agents.add(nm)
            except Exception:
                continue
            if i % 100 == 0:
                print(f"[mods] processed {i}/{len(to_mod)}")

    # 6) Agent profiles (optional)
    if fetch_agent_profiles and seen_agents:
        names = sorted(seen_agents)
        if profile_limit and len(names) > profile_limit:
            names = names[:profile_limit]
        print(f"[agents] fetching profiles for {len(names)} agents (PROFILE_LIMIT={profile_limit or 'none'})")

        for i, name in enumerate(names, 1):
            try:
                prof = client.get_agent_profile(name)
                agent_obj = prof.get("agent", {}) or {}
                if agent_obj:
                    upsert_agents_profile_aware(store, [agent_obj], observed_at)
            except Exception:
                continue
            if i % 200 == 0:
                print(f"[agents] profiled {i}/{len(names)}")

    # 7) Optional HTML scrape
    if scrape_html and seen_agents:
        from html_scrape import scrape_agent_page

        print(f"[html] scraping {len(seen_agents)} agents")
        for name in sorted(seen_agents):
            try:
                info = scrape_agent_page(name)
                if info.get("owner_x_handle"):
                    store.upsert_x_owner(name, info["owner_x_handle"], info.get("owner_x_url"), observed_at)
                if info.get("similar_agents"):
                    store.upsert_similar(name, info["similar_agents"], observed_at, source="html_profile")
            except Exception:
                continue

    # 8) Feed snapshot (auth)
    try:
        feed = client.get_feed(sort="hot", limit=100, offset=0)
        feed_posts = _as_list(feed, "posts", "data")
        for p in feed_posts:
            smn = submolt_name(p.get("submolt"))
            if smn:
                p["submolt"] = smn
        store.write_feed_snapshot(crawl_id, "hot", feed_posts, observed_at)
    except Exception:
        pass

    store.end_crawl(crawl_id)
    store.close()
    print(f"✅ Full crawl done. crawl_id={crawl_id} cutoff={cutoff}")


if __name__ == "__main__":
    main()
