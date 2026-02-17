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
    # Moltbook posts return submolt as {id,name,display_name}
    if isinstance(sub, dict):
        return sub.get("name")
    return sub

def norm_post_for_store(p):
    # Ensure p["submolt"] is a string name; keep original dict fields untouched otherwise
    p = dict(p)
    p["submolt"] = norm_submolt_name(p.get("submolt"))
    return p

def extract_author_name(obj):
    # posts/comments have nested author dict
    a = obj.get("author")
    if isinstance(a, dict):
        return a.get("name")
    return None

def collect_authors_from_comments(tree, out_set):
    # comments are a list; each comment may contain replies list
    for c in tree or []:
        name = extract_author_name(c)
        if name:
            out_set.add(name)
        for r in c.get("replies", []) or []:
            collect_authors_from_comments([r], out_set)

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

    fetch_post_details = os.getenv("FETCH_POST_DETAILS", "0") == "1"
    scrape_html = os.getenv("SCRAPE_AGENT_HTML", "0") == "1"

    # Controls (optional)
    crawl_comments = os.getenv("CRAWL_COMMENTS", "1") == "1"  # set to 0 if you want faster first run
    comments_limit_per_post = int(os.getenv("COMMENTS_LIMIT_PER_POST", "100"))

    store.begin_crawl(crawl_id, mode="full", cutoff_iso=cutoff)

    # 1) Save "me"
    me = client.get_me()
    store.upsert_agents([me], observed_at)

    # 2) Crawl submolts (paginated)
    page = 100
    offset = store.get_checkpoint(crawl_id, "submolts_offset")
    submolts_all = []

    print(f"[submolts] starting offset={offset}")
    while True:
        resp = client.list_submolts(limit=page, offset=offset, sort="popular")
        batch = resp.get("submolts", []) or []
        if not batch:
            break

        submolts_all.extend(batch)
        offset += page
        store.set_checkpoint(crawl_id, "submolts_offset", offset)

        print(f"[submolts] fetched={len(submolts_all)} next_offset={offset}")

        if len(batch) < page:
            break

    # Enrich each submolt via GET /submolts/:name
    submolts_enriched = []
    for s in submolts_all:
        name = s.get("name")
        if not name:
            continue
        try:
            submolts_enriched.append(client.get_submolt(name))
        except Exception:
            submolts_enriched.append(s)

    store.upsert_submolts(submolts_enriched, observed_at)

    # Moderators per submolt
    for s in submolts_enriched:
        name = s.get("name")
        if not name:
            continue
        try:
            mods = client.get_moderators(name)
            store.upsert_moderators_for_submolt(name, mods, observed_at)
            store.upsert_agents(mods, observed_at)
        except Exception:
            continue

    # 3) Crawl ALL posts (paginated)
    all_posts = []
    page = 100
    offset = store.get_checkpoint(crawl_id, "posts_offset")

    print(f"[posts] starting offset={offset}")
    while True:
        resp = client.list_posts(sort="new", limit=page, offset=offset)
        batch = resp.get("posts", []) or []
        if not batch:
            break

        all_posts.extend(batch)

        # checkpoint offset after successful fetch
        if has_more(resp):
            offset = next_offset(resp, offset, page)
            store.set_checkpoint(crawl_id, "posts_offset", offset)

        print(f"[posts] fetched={len(all_posts)} has_more={has_more(resp)} next_offset={offset}")

        if not has_more(resp):
            break

    # Optionally enrich posts via /posts/:id
    if fetch_post_details:
        enriched = []
        for i, p in enumerate(all_posts, 1):
            pid = p.get("id")
            if not pid:
                continue
            try:
                enriched.append(client.get_post(pid))
            except Exception:
                enriched.append(p)
            if i % 200 == 0:
                print(f"[posts] enriched {i}/{len(all_posts)}")
        all_posts = enriched

    # Normalize submolt dict -> name before upserting
    all_posts_norm = [norm_post_for_store(p) for p in all_posts]
    store.upsert_posts(all_posts_norm, observed_at)

    # 4) Comments for posts + collect agent names properly
    seen_agents = set()

    # Authors from posts
    for p in all_posts:
        n = extract_author_name(p)
        if n:
            seen_agents.add(n)

    if crawl_comments:
        print(f"[comments] crawling comments for {len(all_posts)} posts (limit/post={comments_limit_per_post})")
        for i, p in enumerate(all_posts, 1):
            pid = p.get("id")
            if not pid:
                continue
            try:
                tree = client.get_comments(pid, sort="new", limit=comments_limit_per_post)  # returns list
                if tree:
                    store.upsert_comments(pid, tree, observed_at)
                    collect_authors_from_comments(tree, seen_agents)
            except Exception:
                pass

            if i % 200 == 0:
                print(f"[comments] processed {i}/{len(all_posts)} posts; discovered_agents={len(seen_agents)}")

    # 5) Agent profiles for discovered agents
    print(f"[agents] fetching profiles for {len(seen_agents)} agents")
    for i, name in enumerate(sorted(seen_agents), 1):
        try:
            prof = client.get_agent_profile(name)
            agent_obj = prof.get("agent", {}) or {}
            store.upsert_agents([agent_obj], observed_at)
        except Exception:
            continue
        if i % 200 == 0:
            print(f"[agents] profiled {i}/{len(seen_agents)}")

    # 6) Optional HTML scrape
    if scrape_html:
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

    # 7) Feed snapshot — your API likely returns "posts" not "data"
    try:
        feed = client.get_feed(sort="hot", limit=100, offset=0)
        feed_posts = feed.get("posts", []) or feed.get("data", []) or []
        store.write_feed_snapshot(crawl_id, "hot", feed_posts, observed_at)
    except Exception:
        pass

    store.end_crawl(crawl_id)
    store.close()
    print(f"✅ Full crawl done. crawl_id={crawl_id} cutoff={cutoff}")

if __name__ == "__main__":
    main()
