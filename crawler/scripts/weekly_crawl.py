# crawler/scripts/weekly_crawl.py
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


def parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _as_list(resp: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
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
    q = dict(p)
    q["submolt"] = submolt_name(q.get("submolt"))
    return q


def extract_author_name(obj: Dict[str, Any]) -> Optional[str]:
    a = obj.get("author")
    if isinstance(a, dict):
        return a.get("name")
    if isinstance(obj.get("author_name"), str):
        return obj.get("author_name")
    return None


def collect_authors_from_comments(tree: List[Dict[str, Any]], out_set: Set[str]) -> None:
    for c in tree or []:
        n = extract_author_name(c)
        if n:
            out_set.add(n)
        replies = c.get("replies") or []
        if isinstance(replies, list) and replies:
            collect_authors_from_comments(replies, out_set)


def upsert_agents_profile_aware(store: Neo4jStore, agents: List[Dict[str, Any]], obs: str) -> None:
    if not agents:
        return
    try:
        sig = inspect.signature(store.upsert_agents)
        if "mark_profile" in sig.parameters:
            store.upsert_agents(agents, obs, mark_profile=True)  # type: ignore
        else:
            store.upsert_agents(agents, obs)
    except TypeError:
        store.upsert_agents(agents, obs)


# --------------------------
# Public GET wrapper (no Authorization header)
# --------------------------
def public_get_json(client: MoltbookClient, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        try:
            client._sleep_if_needed()  # type: ignore[attr-defined]
        except Exception:
            pass

        r = requests.get(url, headers=headers, params=params, timeout=60)

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

        if r.status_code == 401:
            raise PermissionError("401 Unauthorized on public_get_json")

        r.raise_for_status()
        return r.json()

    r.raise_for_status()
    return {}


def get_comments_any(client: MoltbookClient, post_id: str, sort: str, limit: int) -> List[Dict[str, Any]]:
    params = {"sort": sort, "limit": limit, "shuffle": int(time.time() * 1000)}
    try:
        resp = public_get_json(client, f"/posts/{post_id}/comments", params=params)
    except PermissionError:
        resp = client._req("GET", f"/posts/{post_id}/comments", params={"sort": sort, "limit": limit})  # type: ignore[attr-defined]

    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        v = resp.get("comments") or resp.get("data") or []
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

    crawl_id = f"weekly:{uuid.uuid4()}"
    cutoff = iso_now()
    observed_at = iso_now()

    fetch_post_details = os.getenv("FETCH_POST_DETAILS", "0") == "1"
    scrape_html = os.getenv("SCRAPE_AGENT_HTML", "0") == "1"

    crawl_comments = os.getenv("CRAWL_COMMENTS", "1") == "1"
    comments_limit_per_post = int(os.getenv("COMMENTS_LIMIT_PER_POST", "500"))

    # /submolts offset pagination is broken in production -> top slice only
    submolt_top_limit = int(os.getenv("SUBMOLT_TOP_LIMIT", "100"))
    refresh_moderators = os.getenv("REFRESH_MODERATORS", "1") == "1"
    moderators_limit = int(os.getenv("MODERATOR_SUBMOLTS_LIMIT", "500"))

    # profile refresh knobs
    stale_limit = int(os.getenv("PROFILE_REFRESH_LIMIT", "500"))
    stale_days = int(os.getenv("PROFILE_REFRESH_DAYS", "7"))

    # paging knobs for posts
    page = int(os.getenv("POSTS_PAGE_SIZE", "50"))
    max_repeat_pages = int(os.getenv("MAX_REPEAT_PAGES", "2"))
    max_stale_pages = int(os.getenv("MAX_STALE_PAGES", "4"))

    last_cutoff_str = store.get_latest_crawl_cutoff()
    last_cutoff_dt = parse_dt(last_cutoff_str) if last_cutoff_str else None

    store.begin_crawl(crawl_id, mode="weekly", cutoff_iso=cutoff)

    # 1) Refresh submolts top slice (public)
    top_submolts: List[Dict[str, Any]] = []
    if submolt_top_limit > 0:
        try:
            resp = public_get_json(
                client,
                "/submolts",
                params={"sort": "popular", "limit": submolt_top_limit, "offset": 0, "shuffle": int(time.time() * 1000)},
            )
            top_submolts = _as_list(resp, "submolts", "data")
            if top_submolts:
                store.upsert_submolts(top_submolts, observed_at)
                print(f"[submolts] refreshed top slice: {len(top_submolts)}")
        except Exception as e:
            print(f"[submolts] top slice refresh failed: {e}")

    # 2) Pull new posts since last cutoff (public + repeat detection)
    new_posts: List[Dict[str, Any]] = []
    offset = 0
    prev_sig = None
    repeat_pages = 0
    stale_pages = 0

    print(f"[posts] last_cutoff={last_cutoff_str} page={page}")

    while True:
        params = {"sort": "new", "limit": page, "offset": offset, "shuffle": int(time.time() * 1000)}
        try:
            resp = public_get_json(client, "/posts", params=params)
        except PermissionError:
            resp = client.list_posts(sort="new", limit=page, offset=offset)

        batch = _as_list(resp, "posts", "data")
        if not batch:
            break

        sig = tuple(p.get("id") for p in batch[:10])
        if sig == prev_sig:
            repeat_pages += 1
        else:
            repeat_pages = 0
        prev_sig = sig

        # optional per-post detail fetch
        if fetch_post_details:
            enriched = []
            for p in batch:
                pid = p.get("id")
                if not pid:
                    continue
                try:
                    det = public_get_json(client, f"/posts/{pid}", params={"shuffle": int(time.time() * 1000)})
                    enriched.append(det.get("post") or p)
                except Exception:
                    enriched.append(p)
            batch = enriched

        # cutoff filtering
        keep_this_page: List[Dict[str, Any]] = []
        saw_old = False
        if last_cutoff_dt:
            for p in batch:
                pdt = parse_dt(p.get("created_at"))
                if pdt and pdt > last_cutoff_dt:
                    keep_this_page.append(p)
                elif pdt and pdt <= last_cutoff_dt:
                    saw_old = True
        else:
            keep_this_page = batch

        new_posts.extend(keep_this_page)

        # stale-page heuristic (if we are not adding anything, offset might be ignored)
        if len(keep_this_page) == 0 and last_cutoff_dt:
            stale_pages += 1
        else:
            stale_pages = 0

        # advance offset safely
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

        print(f"[posts] page_keep={len(keep_this_page)} total_new={len(new_posts)} offset:{old_offset}->{offset} repeat={repeat_pages} stale={stale_pages}")

        # stop conditions
        if saw_old:
            break
        if repeat_pages >= max_repeat_pages:
            print("[posts] WARNING: repeating same page signature; stopping weekly post scan.")
            break
        if stale_pages >= max_stale_pages:
            print("[posts] WARNING: no new posts added for several pages; offset likely ignored; stopping.")
            break
        if not resp.get("has_more"):
            break

    print(f"[posts] new_posts={len(new_posts)}")

    # 2b) Upsert posts
    if new_posts:
        store.upsert_posts([norm_post_for_store(p) for p in new_posts], observed_at)

    # 2c) Discover+upsert submolts from new posts
    submolts_seen: Dict[str, Dict[str, Any]] = {}
    for p in new_posts:
        sm = p.get("submolt")
        smn = submolt_name(sm)
        if not smn:
            continue
        if isinstance(sm, dict):
            prev = submolts_seen.get(smn, {})
            submolts_seen[smn] = {**prev, **sm, "name": smn}
        else:
            submolts_seen.setdefault(smn, {"name": smn})

    discovered_submolts = list(submolts_seen.values())
    if discovered_submolts:
        store.upsert_submolts(discovered_submolts, observed_at)
        print(f"[submolts] discovered from new posts: {len(discovered_submolts)}")

    # 3) Comments + agent discovery
    seen_agents: Set[str] = set()
    for p in new_posts:
        n = extract_author_name(p)
        if n:
            seen_agents.add(n)

    if crawl_comments and new_posts:
        print(f"[comments] crawling for {len(new_posts)} posts (limit/post={comments_limit_per_post})")
        for p in new_posts:
            pid = p.get("id")
            if not pid:
                continue
            try:
                tree = get_comments_any(client, pid, sort="new", limit=comments_limit_per_post)
                if tree:
                    store.upsert_comments(pid, tree, observed_at)
                    collect_authors_from_comments(tree, seen_agents)
            except Exception:
                continue

    # 4) Moderators refresh (bounded), using discovered+top submolts
    if refresh_moderators:
        names = []
        names.extend([s.get("name") for s in discovered_submolts if s.get("name")])
        names.extend([s.get("name") for s in top_submolts if s.get("name")])

        seen = set()
        uniq = []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)

        if moderators_limit > 0:
            uniq = uniq[:moderators_limit]

        print(f"[mods] refreshing moderators for {len(uniq)} submolts")
        for i, name in enumerate(uniq, 1):
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
                        mn = m.get("name") or m.get("agent_name")
                        if not mn:
                            af = m.get("agent")
                            if isinstance(af, str):
                                mn = af
                            elif isinstance(af, dict):
                                mn = af.get("name")
                        if isinstance(mn, str) and mn:
                            seen_agents.add(mn)
            except Exception:
                continue
            if i % 100 == 0:
                print(f"[mods] processed {i}/{len(uniq)}")

    # 5) Agent profile refresh (newly seen + stale from DB)
    names_to_refresh = set(seen_agents)
    try:
        stale = store.get_agents_needing_profile_refresh(days=stale_days, limit=stale_limit)
        names_to_refresh.update(stale)
    except Exception:
        pass

    if names_to_refresh:
        print(f"[agents] refreshing profiles for {len(names_to_refresh)} agents")
        for name in sorted(names_to_refresh):
            try:
                prof = client.get_agent_profile(name)
                agent_obj = prof.get("agent", {}) or {}
                if agent_obj:
                    upsert_agents_profile_aware(store, [agent_obj], observed_at)
            except Exception:
                continue

    # 6) Optional HTML scrape (only for newly seen agents)
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

    # 7) Feed snapshot
    try:
        feed = client.get_feed(sort="hot", limit=100, offset=0)
        feed_posts = feed.get("posts", []) or feed.get("data", []) or []
        for p in feed_posts:
            p["submolt"] = submolt_name(p.get("submolt"))
        store.write_feed_snapshot(crawl_id, "hot", feed_posts, observed_at)
    except Exception:
        pass

    store.end_crawl(crawl_id)
    store.close()
    print(f"✅ Weekly crawl done. crawl_id={crawl_id} last_cutoff={last_cutoff_str}")


if __name__ == "__main__":
    main()
