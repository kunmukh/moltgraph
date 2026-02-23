import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from typing import List
    
from neo4j import GraphDatabase

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def chunked(xs: List[Dict[str, Any]], n: int) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(xs), n):
        yield xs[i:i+n]

def flatten_comments(tree: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []

    def rec(node: Dict[str, Any], parent_id: Optional[str]):
        replies = node.get("replies", []) or []
        n = dict(node)
        n["parent_id"] = n.get("parent_id") or parent_id
        n.pop("replies", None)
        flat.append(n)
        for r in replies:
            rec(r, n["id"])

    for c in tree:
        rec(c, c.get("parent_id"))
    return flat

class Neo4jStore:
    def __init__(self, uri: str, user: str, pwd: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, pwd))

    def close(self):
        self.driver.close()
        
    def _submolt_name(self, sub):
        return sub.get("name") if isinstance(sub, dict) else sub

    # checkpoint
    def get_checkpoint(self, crawl_id: str, prop: str) -> int:
        q = """
        MATCH (cr:Crawl {id:$id})
        RETURN coalesce(cr[$prop], 0) AS v
        """
        with self.driver.session() as s:
            rec = s.run(q, id=crawl_id, prop=prop).single()
            return int(rec["v"]) if rec and rec["v"] is not None else 0

    def set_checkpoint(self, crawl_id: str, prop: str, value: int) -> None:
        q = """
        MATCH (cr:Crawl {id:$id})
        SET cr[$prop] = $value,
            cr.last_updated_at = datetime($ts)
        """
        with self.driver.session() as s:
            s.run(q, id=crawl_id, prop=prop, value=value, ts=now_iso())

    # ---- Crawl bookkeeping ----
    def begin_crawl(self, crawl_id: str, mode: str, cutoff_iso: str) -> None:
        q = """
        MERGE (cr:Crawl {id:$id})
        ON CREATE SET cr.started_at = datetime($started_at)
        SET cr.mode = $mode, cr.cutoff = datetime($cutoff), cr.last_updated_at = datetime($started_at)
        """
        with self.driver.session() as s:
            s.run(q, id=crawl_id, mode=mode, cutoff=cutoff_iso, started_at=now_iso())

    def end_crawl(self, crawl_id: str) -> None:
        q = """
        MATCH (cr:Crawl {id:$id})
        SET cr.ended_at = datetime($ended_at), cr.last_updated_at = datetime($ended_at)
        """
        with self.driver.session() as s:
            s.run(q, id=crawl_id, ended_at=now_iso())

    def get_latest_crawl_cutoff(self) -> Optional[str]:
        q = """
        MATCH (cr:Crawl)
        WHERE cr.cutoff IS NOT NULL
        RETURN cr.cutoff AS cutoff
        ORDER BY cr.cutoff DESC
        LIMIT 1
        """
        with self.driver.session() as s:
            r = s.run(q).single()
            if not r:
                return None
            return r["cutoff"].to_native().isoformat()

    def get_agents_needing_profile_refresh(self, days: int = 7, limit: int = 500) -> List[str]:
        """
        Returns agent names whose profile is missing or stale.
        Uses Agent.profile_last_fetched_at (set only when mark_profile=True in upsert_agents).
        """
        q = """
        MATCH (a:Agent)
        WHERE a.name IS NOT NULL
        AND (
            a.profile_last_fetched_at IS NULL OR
            a.profile_last_fetched_at < datetime() - duration({days: $days})
        )
        RETURN a.name AS name
        ORDER BY coalesce(a.profile_last_fetched_at, datetime("1970-01-01T00:00:00Z")) ASC
        LIMIT $limit
        """
        with self.driver.session() as s:
            res = s.run(q, days=int(days), limit=int(limit))
            return [r["name"] for r in res if r and r.get("name")]


    # ---- Upserts ----
    def upsert_agents(self, agents: List[Dict[str, Any]], observed_at_iso: str, mark_profile: bool = False):
        q = """
        UNWIND $rows AS row
        MERGE (a:Agent {name: row.name})
        ON CREATE SET a.first_seen_at = datetime($obs),
                    a.created_at = datetime(coalesce(row.created_at, $obs))
        SET a.last_seen_at = datetime($obs),
            a.display_name = coalesce(row.display_name, a.display_name),
            a.description = coalesce(row.description, a.description),
            a.avatar_url = coalesce(row.avatar_url, a.avatar_url),
            a.status = coalesce(row.status, a.status),
            a.is_claimed = coalesce(row.is_claimed, a.is_claimed),
            a.is_active = coalesce(row.is_active, a.is_active),
            a.karma = coalesce(row.karma, a.karma),
            a.follower_count = coalesce(row.follower_count, a.follower_count),
            a.following_count = coalesce(row.following_count, a.following_count),
            a.owner_twitter_id = coalesce(row.owner_twitter_id, a.owner_twitter_id),
            a.owner_twitter_handle = coalesce(row.owner_twitter_handle, a.owner_twitter_handle),
            a.claimed_at = CASE WHEN row.claimed_at IS NULL THEN a.claimed_at ELSE datetime(row.claimed_at) END,
            a.last_active = CASE WHEN row.last_active IS NULL THEN a.last_active ELSE datetime(row.last_active) END,
            a.updated_at = CASE WHEN row.updated_at IS NULL THEN a.updated_at ELSE datetime(row.updated_at) END,
            a.profile_last_fetched_at = CASE
                WHEN $mark_profile THEN datetime($obs)
                ELSE a.profile_last_fetched_at
            END
        """

        # Normalize keys (API mixes camelCase/snake_case)
        def norm(x: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "name": x.get("name"),
                "display_name": x.get("displayName") or x.get("display_name"),
                "description": x.get("description"),
                "status": x.get("status"),
                "karma": x.get("karma"),
                "owner_twitter_id": x.get("owner_twitter_id"),
                "owner_twitter_handle": x.get("owner_twitter_handle"),
                "updated_at": x.get("updated_at"),
                "claimed_at": x.get("claimed_at"),
                "avatar_url": x.get("avatarUrl") or x.get("avatar_url"),
                "follower_count": x.get("followerCount") or x.get("follower_count"),
                "following_count": x.get("followingCount") or x.get("following_count"),
                "is_claimed": x.get("isClaimed") if "isClaimed" in x else x.get("is_claimed"),
                "is_active": x.get("isActive") if "isActive" in x else x.get("is_active"),
                "created_at": x.get("createdAt") or x.get("created_at"),
                "last_active": x.get("lastActive") or x.get("last_active"),
            }

        rows = [norm(a) for a in agents if a.get("name")]
        with self.driver.session() as s:
            for batch in chunked(rows, 500):
                s.run(q, rows=batch, obs=observed_at_iso, mark_profile=mark_profile)

    # def upsert_x_owner(self, agent_name: str, handle: str, url: Optional[str], observed_at_iso: str):
    #     q = """
    #     MATCH (a:Agent {name:$agent})
    #     MERGE (x:XAccount {handle:$handle})
    #     ON CREATE SET x.first_seen_at=datetime($obs)
    #     SET x.last_seen_at=datetime($obs),
    #         x.url = coalesce($url, x.url)
    #     MERGE (a)-[r:HAS_OWNER_X]->(x)
    #     ON CREATE SET r.first_seen_at=datetime($obs)
    #     SET r.last_seen_at=datetime($obs)
    #     """
    #     with self.driver.session() as s:
    #         s.run(q, agent=agent_name, handle=handle, url=url, obs=observed_at_iso)
    def upsert_x_owner(
        self,
        agent_name: str,
        handle: str,
        url: Optional[str],
        observed_at_iso: str,
        *,
        x_name: Optional[str] = None,
        x_avatar: Optional[str] = None,
        x_bio: Optional[str] = None,
        x_follower_count: Optional[int] = None,
        x_following_count: Optional[int] = None,
        x_verified: Optional[bool] = None,
        ):
        if not isinstance(handle, str) or not handle.strip():
            return
        handle = handle.strip().lstrip("@")

        q = """
        MATCH (a:Agent {name:$agent})
        MERGE (x:XAccount {handle:$handle})
        ON CREATE SET x.first_seen_at=datetime($obs)
        SET x.last_seen_at=datetime($obs),
            x.url = coalesce($url, x.url),
            x.name = coalesce($x_name, x.name),
            x.avatar_url = coalesce($x_avatar, x.avatar_url),
            x.bio = coalesce($x_bio, x.bio),
            x.follower_count = coalesce($x_follower_count, x.follower_count),
            x.following_count = coalesce($x_following_count, x.following_count),
            x.is_verified = coalesce($x_verified, x.is_verified)
        MERGE (a)-[r:HAS_OWNER_X]->(x)
        ON CREATE SET r.first_seen_at=datetime($obs)
        SET r.last_seen_at=datetime($obs)
        """
        with self.driver.session() as s:
            s.run(
                q,
                agent=agent_name,
                handle=handle,
                url=url,
                obs=observed_at_iso,
                x_name=x_name,
                x_avatar=x_avatar,
                x_bio=x_bio,
                x_follower_count=x_follower_count,
                x_following_count=x_following_count,
                x_verified=x_verified,
            )

    def upsert_submolts(self, submolts: List[Dict[str, Any]], observed_at_iso: str):
        q = """
        UNWIND $rows AS row
        MERGE (s:Submolt {name: row.name})
        ON CREATE SET s.first_seen_at=datetime($obs), s.created_at=datetime(coalesce(row.created_at, $obs))
        SET s.last_seen_at=datetime($obs),
            s.display_name = coalesce(row.display_name, s.display_name),
            s.description  = coalesce(row.description, s.description),
            s.avatar_url   = coalesce(row.avatar_url, s.avatar_url),
            s.banner_url   = coalesce(row.banner_url, s.banner_url),
            s.banner_color = coalesce(row.banner_color, s.banner_color),
            s.theme_color  = coalesce(row.theme_color, s.theme_color),
            s.subscriber_count = coalesce(row.subscriber_count, s.subscriber_count),
            s.post_count   = coalesce(row.post_count, s.post_count),
            s.updated_at   = CASE WHEN row.updated_at IS NULL THEN s.updated_at ELSE datetime(row.updated_at) END
        """
        def norm(x: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "name": x.get("name"),
                "display_name": x.get("display_name") or x.get("displayName"),
                "description": x.get("description"),
                "avatar_url": x.get("avatarUrl") or x.get("avatar_url"),
                "banner_url": x.get("bannerUrl") or x.get("banner_url"),
                "banner_color": x.get("bannerColor") or x.get("banner_color"),
                "theme_color": x.get("themeColor") or x.get("theme_color"),
                "subscriber_count": x.get("subscriberCount") or x.get("subscriber_count"),
                "post_count": x.get("postCount") or x.get("post_count"),
                "created_at": x.get("createdAt") or x.get("created_at"),
                "updated_at": x.get("updatedAt") or x.get("updated_at"),
            }

        rows = [norm(s) for s in submolts if s.get("name")]
        with self.driver.session() as sess:
            for batch in chunked(rows, 500):
                sess.run(q, rows=batch, obs=observed_at_iso)

    def upsert_posts(self, posts: List[Dict[str, Any]], observed_at_iso: str):
        q_nodes = """
        UNWIND $rows AS row
        MERGE (p:Post {id: row.id})
        ON CREATE SET p.first_seen_at=datetime($obs),
            p.created_at = CASE WHEN row.created_at IS NULL THEN datetime($obs) ELSE datetime(row.created_at) END
        SET p.last_seen_at=datetime($obs),
            p.title = coalesce(row.title, p.title),
            p.content = coalesce(row.content, p.content),
            p.url = coalesce(row.url, p.url),
            p.submolt = coalesce(row.submolt, p.submolt),
            p.type = coalesce(row.type, p.type),
            p.score = coalesce(row.score, p.score),
            p.upvotes = coalesce(row.upvotes, p.upvotes),
            p.downvotes = coalesce(row.downvotes, p.downvotes),
            p.comment_count = coalesce(row.comment_count, p.comment_count),
            p.hot_score = coalesce(row.hot_score, p.hot_score),
            p.is_pinned = coalesce(row.is_pinned, p.is_pinned),
            p.is_locked = coalesce(row.is_locked, p.is_locked),
            p.is_deleted = coalesce(row.is_deleted, p.is_deleted),
            p.submolt_id = coalesce(row.submolt_id, p.submolt_id),
            p.updated_at = CASE WHEN row.updated_at IS NULL THEN p.updated_at ELSE datetime(row.updated_at) END
        """
        q_rels = """
        UNWIND $rows AS row
        MERGE (a:Agent {name: row.author_name})
        ON CREATE SET a.first_seen_at=datetime($obs)
        SET a.last_seen_at=datetime($obs),
            a.id = coalesce(row.author_id, a.id),
            a.display_name = coalesce(row.author_display_name, a.display_name),
            a.description = coalesce(row.author_description, a.description),
            a.avatar_url = coalesce(row.author_avatar_url, a.avatar_url),
            a.karma = coalesce(row.author_karma, a.karma),
            a.follower_count = coalesce(row.author_follower_count, a.follower_count),
            a.following_count = coalesce(row.author_following_count, a.following_count),
            a.is_claimed = coalesce(row.author_is_claimed, a.is_claimed),
            a.is_active = coalesce(row.author_is_active, a.is_active),
            a.created_at = CASE WHEN row.author_created_at IS NULL THEN a.created_at ELSE datetime(row.author_created_at) END,
            a.last_active = CASE WHEN row.author_last_active IS NULL THEN a.last_active ELSE datetime(row.author_last_active) END

        WITH row, a
        MERGE (s:Submolt {name: row.submolt})
        ON CREATE SET s.first_seen_at=datetime($obs)
        SET s.last_seen_at=datetime($obs)

        WITH row, a, s
        MATCH (p:Post {id: row.id})
        MERGE (a)-[r1:AUTHORED]->(p)
        ON CREATE SET r1.first_seen_at=datetime($obs), r1.created_at = p.created_at
        SET r1.last_seen_at=datetime($obs)

        MERGE (p)-[r2:IN_SUBMOLT]->(s)
        ON CREATE SET r2.first_seen_at=datetime($obs), r2.created_at = p.created_at
        SET r2.last_seen_at=datetime($obs)
        """

        def norm(p: Dict[str, Any]) -> Dict[str, Any]:
            author = p.get("author") or {}
            sub = p.get("submolt")
            submolt_name = sub.get("name") if isinstance(sub, dict) else sub
            return {
                "id": p.get("id"),
                "title": p.get("title"),
                "content": p.get("content"),
                "url": p.get("url"),
                "submolt": submolt_name,
                "submolt_id": sub.get("id") if isinstance(sub, dict) else None,
                "type": p.get("type"),  # 'text'/'link'
                "score": p.get("score"),
                "upvotes": p.get("upvotes"),
                "downvotes": p.get("downvotes"),
                "comment_count": p.get("comment_count"),
                "hot_score": p.get("hot_score"),
                "is_pinned": p.get("is_pinned"),
                "is_locked": p.get("is_locked"),
                "is_deleted": p.get("is_deleted"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),

                # author fields
                "author_id": author.get("id") or p.get("author_id"),
                "author_name": author.get("name") or (p.get("author") if isinstance(p.get("author"), str) else None),
                "author_description": author.get("description"),
                "author_avatar_url": author.get("avatarUrl") or author.get("avatar_url"),
                "author_karma": author.get("karma"),
                "author_follower_count": author.get("followerCount") or author.get("follower_count"),
                "author_following_count": author.get("followingCount") or author.get("following_count"),
                "author_is_claimed": author.get("isClaimed") if "isClaimed" in author else author.get("is_claimed"),
                "author_is_active": author.get("isActive") if "isActive" in author else author.get("is_active"),
                "author_created_at": author.get("createdAt") or author.get("created_at"),
                "author_last_active": author.get("lastActive") or author.get("last_active"),
                "author_display_name": author.get("displayName") or author.get("display_name"),
            }

        tmp = [norm(p) for p in posts if p.get("id") and p.get("created_at")]
        rows = [r for r in tmp if r.get("author_name") and r.get("submolt")]
        with self.driver.session() as s:
            for batch in chunked(rows, 300):
                s.run(q_nodes, rows=batch, obs=observed_at_iso)
                s.run(q_rels, rows=batch, obs=observed_at_iso)

    def upsert_comments(self, post_id: str, comments_tree: List[Dict[str, Any]], observed_at_iso: str):
        # comments_tree is a LIST. Each comment may include nested replies.
        flat = flatten_comments(comments_tree)
        for c in flat:
            c["post_id"] = c.get("post_id") or post_id

        q_nodes = """
        UNWIND $rows AS row
        MERGE (c:Comment {id: row.id})
        ON CREATE SET c.first_seen_at=datetime($obs), c.created_at=datetime(row.created_at)
        SET c.last_seen_at=datetime($obs),
            c.content = coalesce(row.content, c.content),
            c.score = coalesce(row.score, c.score),
            c.upvotes = coalesce(row.upvotes, c.upvotes),
            c.downvotes = coalesce(row.downvotes, c.downvotes),
            c.reply_count = coalesce(row.reply_count, c.reply_count),
            c.is_deleted = coalesce(row.is_deleted, c.is_deleted),
            c.depth = coalesce(row.depth, c.depth),
            c.updated_at = CASE WHEN row.updated_at IS NULL THEN c.updated_at ELSE datetime(row.updated_at) END
        """

        q_rels = """
        UNWIND $rows AS row
        MERGE (a:Agent {name: row.author_name})
        ON CREATE SET a.first_seen_at=datetime($obs)
        SET a.last_seen_at=datetime($obs),
            a.id = coalesce(row.author_id, a.id),
            a.description = coalesce(row.author_description, a.description),
            a.avatar_url = coalesce(row.author_avatar_url, a.avatar_url),
            a.karma = coalesce(row.author_karma, a.karma),
            a.follower_count = coalesce(row.author_follower_count, a.follower_count),
            a.following_count = coalesce(row.author_following_count, a.following_count),
            a.is_claimed = coalesce(row.author_is_claimed, a.is_claimed),
            a.is_active = coalesce(row.author_is_active, a.is_active),
            a.created_at = CASE WHEN row.author_created_at IS NULL THEN a.created_at ELSE datetime(row.author_created_at) END,
            a.last_active = CASE WHEN row.author_last_active IS NULL THEN a.last_active ELSE datetime(row.author_last_active) END

        WITH row, a
        MATCH (c:Comment {id: row.id})
        MATCH (p:Post {id: row.post_id})

        MERGE (a)-[r1:AUTHORED]->(c)
        ON CREATE SET r1.first_seen_at=datetime($obs), r1.created_at = c.created_at
        SET r1.last_seen_at=datetime($obs)

        MERGE (c)-[r2:ON_POST]->(p)
        ON CREATE SET r2.first_seen_at=datetime($obs), r2.created_at = c.created_at
        SET r2.last_seen_at=datetime($obs)

        WITH row, c
        WHERE row.parent_id IS NOT NULL
        MATCH (parent:Comment {id: row.parent_id})
        MERGE (c)-[r3:REPLY_TO]->(parent)
        ON CREATE SET r3.first_seen_at=datetime($obs), r3.created_at = c.created_at
        SET r3.last_seen_at=datetime($obs)
        """


        def norm(x: Dict[str, Any]) -> Dict[str, Any]:
            author = x.get("author") or {}
            return {
                "id": x.get("id"),
                "post_id": x.get("post_id"),
                "content": x.get("content"),
                "upvotes": x.get("upvotes"),
                "downvotes": x.get("downvotes"),
                "score": x.get("score"),
                "reply_count": x.get("reply_count"),
                "is_deleted": x.get("is_deleted"),
                "depth": x.get("depth"),
                "created_at": x.get("created_at"),
                "updated_at": x.get("updated_at"),

                # flatten_comments provides parent_id if we pass it through
                "parent_id": x.get("parent_id"),

                # author nested
                "author_id": author.get("id") or x.get("author_id"),
                "author_name": author.get("name"),
                "author_description": author.get("description"),
                "author_avatar_url": author.get("avatarUrl") or author.get("avatar_url"),
                "author_karma": author.get("karma"),
                "author_follower_count": author.get("followerCount") or author.get("follower_count"),
                "author_following_count": author.get("followingCount") or author.get("following_count"),
                "author_is_claimed": author.get("isClaimed") if "isClaimed" in author else author.get("is_claimed"),
                "author_is_active": author.get("isActive") if "isActive" in author else author.get("is_active"),
                "author_created_at": author.get("createdAt") or author.get("created_at"),
                "author_last_active": author.get("lastActive") or author.get("last_active"),
            }

        rows = [norm(c) for c in flat if c.get("id") and c.get("created_at")]
        rows = [r for r in rows if r.get("author_name") and (r.get("post_id") or post_id)]
        # rows = [norm(c) for c in flat if c.get("id") and c.get("created_at") and (c.get("post_id") or post_id)]
        with self.driver.session() as s:
            for batch in chunked(rows, 500):
                s.run(q_nodes, rows=batch, obs=observed_at_iso)
                s.run(q_rels, rows=batch, obs=observed_at_iso)

    def upsert_moderators_for_submolt(self, submolt_name: str, moderators: List[Dict[str, Any]], observed_at_iso: str):
        # Best-effort normalization (the API returns {moderators:[...]} but exact keys can evolve)
        # IMPORTANT: Some endpoints return wrapper objects like {"role": "...", "agent": {<full agent dict>}}
        # In that case, m["agent"] is a dict, and we MUST extract agent["name"] (Neo4j properties can't store maps).
        current_names: List[str] = []
        rows: List[Dict[str, Any]] = []

        for m in moderators or []:
            if not isinstance(m, dict):
                continue

            role = m.get("role", "moderator")

            # Common shapes:
            #  (A) {"name": "Alice", "role": "..."}
            #  (B) {"agent_name": "Alice", "role": "..."}
            #  (C) {"agent": "Alice", "role": "..."}
            #  (D) {"agent": {"name": "Alice", "displayName": "...", ...}, "role": "..."}
            agent_field = m.get("agent")

            name = m.get("name") or m.get("agent_name")
            display_name = m.get("display_name") or m.get("displayName")

            if not name:
                if isinstance(agent_field, str):
                    name = agent_field
                elif isinstance(agent_field, dict):
                    name = agent_field.get("name") or agent_field.get("agent_name")
                    display_name = display_name or agent_field.get("displayName") or agent_field.get("display_name")

            if not isinstance(name, str) or not name:
                continue

            current_names.append(name)
            rows.append({
                "name": name,
                "display_name": display_name,
                "role": role,
            })
        q_end_missing = """
        MATCH (s:Submolt {name:$submolt})
        OPTIONAL MATCH (a:Agent)-[r:MODERATES]->(s)
        WHERE r.ended_at IS NULL AND NOT a.name IN $current
        SET r.ended_at=datetime($obs), r.last_seen_at=datetime($obs)
        """
        q_merge = """
        UNWIND $rows AS row
        MERGE (s:Submolt {name:$submolt})
        ON CREATE SET s.first_seen_at=datetime($obs)
        SET s.last_seen_at=datetime($obs)

        MERGE (a:Agent {name: row.name})
        ON CREATE SET a.first_seen_at=datetime($obs)
        SET a.last_seen_at=datetime($obs),
            a.display_name = coalesce(row.display_name, a.display_name)

        MERGE (a)-[r:MODERATES]->(s)
        ON CREATE SET r.first_seen_at=datetime($obs)
        SET r.last_seen_at=datetime($obs),
            r.role = coalesce(row.role, r.role),
            r.ended_at = NULL
        """
        with self.driver.session() as s:
            s.run(q_end_missing, submolt=submolt_name, current=current_names, obs=observed_at_iso)
            s.run(q_merge, submolt=submolt_name, rows=rows, obs=observed_at_iso)

    def upsert_similar(self, agent_name: str, similar_names: List[str], observed_at_iso: str, source: str="html_profile"):
        # End old edges not present now, then merge current.
        q_end_missing = """
        MATCH (a:Agent {name:$agent})
        OPTIONAL MATCH (a)-[r:SIMILAR_TO {source:$source}]->(b:Agent)
        WHERE r.ended_at IS NULL AND NOT b.name IN $current
        SET r.ended_at=datetime($obs), r.last_seen_at=datetime($obs)
        """
        q_merge = """
        UNWIND $rows AS row
        MERGE (a:Agent {name:$agent})
        ON CREATE SET a.first_seen_at=datetime($obs)
        SET a.last_seen_at=datetime($obs)

        MERGE (b:Agent {name: row.other})
        ON CREATE SET b.first_seen_at=datetime($obs)
        SET b.last_seen_at=datetime($obs)

        MERGE (a)-[r:SIMILAR_TO {source:$source}]->(b)
        ON CREATE SET r.first_seen_at=datetime($obs)
        SET r.last_seen_at=datetime($obs),
            r.ended_at = NULL
        """
        rows = [{"other": n} for n in sorted(set(similar_names)) if n and n != agent_name]
        with self.driver.session() as s:
            s.run(q_end_missing, agent=agent_name, source=source, current=[r["other"] for r in rows], obs=observed_at_iso)
            if rows:
                s.run(q_merge, agent=agent_name, rows=rows, source=source, obs=observed_at_iso)

    def write_feed_snapshot(self, crawl_id: str, sort: str, posts: List[Dict[str, Any]], observed_at_iso: str):
        fs_id = f"{crawl_id}:{sort}"
        q = """
        MERGE (fs:FeedSnapshot {id:$id})
        ON CREATE SET fs.first_seen_at=datetime($obs), fs.observed_at=datetime($obs)
        SET fs.last_seen_at=datetime($obs),
            fs.sort = $sort

        WITH fs
        UNWIND $rows AS row
        MERGE (p:Post {id: row.id})
        ON CREATE SET p.first_seen_at=datetime($obs), p.created_at=datetime(row.created_at)
        SET p.last_seen_at=datetime($obs),
            p.title = coalesce(row.title, p.title),
            p.submolt = coalesce(row.submolt, p.submolt),
            p.score = coalesce(row.score, p.score)

        MERGE (fs)-[r:CONTAINS]->(p)
        ON CREATE SET r.first_seen_at=datetime($obs)
        SET r.last_seen_at=datetime($obs),
            r.rank = row.rank
        """
        rows = []
        for i, p in enumerate(posts):
            rows.append({
                "id": p.get("id"),
                "title": p.get("title"),
                "submolt": self._submolt_name(p.get("submolt")),
                "score": p.get("score"),
                "created_at": p.get("created_at"),
                "rank": i+1,
            })
        with self.driver.session() as s:
            s.run(q, id=fs_id, sort=sort, rows=[r for r in rows if r["id"]], obs=observed_at_iso)
