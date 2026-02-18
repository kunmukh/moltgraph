import os
import uuid
from datetime import datetime, timezone

from moltbook_client import MoltbookClient
from neo4j_store import Neo4jStore

def iso_now():
    return datetime.now(timezone.utc).isoformat()

def has_more(resp):
    return bool(resp.get("has_more"))

def next_offset(resp, current, page_size):
    return int(resp.get("next_offset", current + page_size))

def norm_submolt_name(sub):
    if isinstance(sub, dict):
        return sub.get("name")
    return sub

def norm_post_for_store(p):
    p = dict(p)
    p["submolt"] = norm_submolt_name(p.get("submolt"))
    return p

def extract_author_name(obj):
    a = obj.get("author")
    if isinstance(a, dict):
        return a.get("name")
    return None

def collect_authors_from_comments(tree, out_set):
    for c in tree or []:
        n = extract_author_name(c)
        if n:
            out_set.add(n)
        for r in c.get("replies", []) or []:
            collect_authors_from_comments([r], out_set)

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

    # Optional knobs
    crawl_comments = os.getenv("CRAWL_COMMENTS", "1") == "1"
    comments_limit_per_post = int(os.getenv("COMMENTS_LIMIT_PER_POST", "500"))

    # Stop condition: posts are ordered newest-first for sort=new
    last_cutoff = store.get_latest_crawl_cutoff()

    store.begin_crawl(crawl_id, mode="weekly", cutoff_iso=cutoff)

    # 1) Refresh submolts + moderators
    submolts = []
    offset = 0
    page = 100

    while True:
        resp = client.list_submolts(limit=page, offset=offset, sort="popular")
        batch = resp.get("submolts", []) or []
        if not batch:
            break
        submolts.extend(batch)
        if len(batch) < page:
            break
        offset += page

    enriched = []
    for s in submolts:
        name = s.get("name")
        if not name:
            continue
        try:
            enriched.append(client.get_submolt(name))
        except Exception:
            enriched.append(s)

    store.upsert_submolts(enriched, observed_at)

    for s in enriched:
        name = s.get("name")
        if not name:
            continue
        try:
            mods = client.get_moderators(name)
            store.upsert_moderators_for_submolt(name, mods, observed_at)
            store.upsert_agents(mods, observed_at)
        except Exception:
            continue

    # 2) Pull new posts since last crawl cutoff
    new_posts = []
    offset = 0
    page = 100

    while True:
        resp = client.list_posts(sort="new", limit=page, offset=offset)
        batch = resp.get("posts", []) or []
        if not batch:
            break

        if last_cutoff:
            keep = [p for p in batch if p.get("created_at") and p["created_at"] > last_cutoff]
            new_posts.extend(keep)
            # stop early once we hit older posts
            if len(keep) == 0:
                break
        else:
            new_posts.extend(batch)

        if not has_more(resp):
            break
        offset = next_offset(resp, offset, page)

    if fetch_post_details and new_posts:
        enriched_posts = []
        for p in new_posts:
            pid = p.get("id")
            if not pid:
                continue
            try:
                enriched_posts.append(client.get_post(pid))
            except Exception:
                enriched_posts.append(p)
        new_posts = enriched_posts

    # Normalize submolt dict -> name before upserting
    new_posts_norm = [norm_post_for_store(p) for p in new_posts]
    if new_posts_norm:
        store.upsert_posts(new_posts_norm, observed_at)

    # 3) Comments + agent discovery (for new posts)
    seen_agents = set()

    for p in new_posts:
        n = extract_author_name(p)
        if n:
            seen_agents.add(n)

    if crawl_comments:
        for p in new_posts:
            pid = p.get("id")
            if not pid:
                continue
            try:
                tree = client.get_comments(pid, sort="new", limit=comments_limit_per_post)  # list
                if tree:
                    store.upsert_comments(pid, tree, observed_at)
                    collect_authors_from_comments(tree, seen_agents)
            except Exception:
                continue

    # 4) Update agent profiles for newly observed agents
    # Update agent profiles:
    #  (A) Always profile-refresh agents we just discovered this week
    #  (B) Also refresh any stale profiles already in DB
    stale_limit = int(os.getenv("PROFILE_REFRESH_LIMIT", "500"))
    stale_days  = int(os.getenv("PROFILE_REFRESH_DAYS", "7"))

    names_to_refresh = set(seen_agents)

    # refresh stale agents from DB
    try:
        stale = store.get_agents_needing_profile_refresh(days=stale_days, limit=stale_limit)
        names_to_refresh.update(stale)
    except Exception:
        pass

    for name in sorted(names_to_refresh):
        try:
            prof = client.get_agent_profile(name)
            agent_obj = prof.get("agent", {}) or {}
            store.upsert_agents([agent_obj], observed_at, mark_profile=True)
        except Exception:
            continue

    # 5) Optional: scrape UI-only Similar + Owner X
    if scrape_html and seen_agents:
        from html_scrape import scrape_agent_page
        for name in sorted(seen_agents):
            try:
                info = scrape_agent_page(name)
                if info.get("owner_x_handle"):
                    store.upsert_x_owner(name, info["owner_x_handle"], info.get("owner_x_url"), observed_at)
                if info.get("similar_agents"):
                    store.upsert_similar(name, info["similar_agents"], observed_at, source="html_profile")
            except Exception:
                continue

    # 6) Snapshot feed (API tends to return "posts")
    try:
        feed = client.get_feed(sort="hot", limit=100, offset=0)
        feed_posts = feed.get("posts", []) or feed.get("data", []) or []
        store.write_feed_snapshot(crawl_id, "hot", feed_posts, observed_at)
    except Exception:
        pass

    store.end_crawl(crawl_id)
    store.close()
    print(f"✅ Weekly crawl done. crawl_id={crawl_id} last_cutoff={last_cutoff}")

if __name__ == "__main__":
    main()
