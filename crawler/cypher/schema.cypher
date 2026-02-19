// --------------------
// Constraints
// --------------------
CREATE CONSTRAINT agent_name_unique IF NOT EXISTS
FOR (a:Agent) REQUIRE a.name IS UNIQUE;

CREATE CONSTRAINT submolt_name_unique IF NOT EXISTS
FOR (s:Submolt) REQUIRE s.name IS UNIQUE;

CREATE CONSTRAINT post_id_unique IF NOT EXISTS
FOR (p:Post) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT comment_id_unique IF NOT EXISTS
FOR (c:Comment) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT crawl_id_unique IF NOT EXISTS
FOR (cr:Crawl) REQUIRE cr.id IS UNIQUE;

CREATE CONSTRAINT feedsnapshot_id_unique IF NOT EXISTS
FOR (fs:FeedSnapshot) REQUIRE fs.id IS UNIQUE;

CREATE CONSTRAINT x_handle_unique IF NOT EXISTS
FOR (x:XAccount) REQUIRE x.handle IS UNIQUE;

// --------------------
// Helpful indexes
// --------------------
CREATE INDEX post_created_at IF NOT EXISTS
FOR (p:Post) ON (p.created_at);

CREATE INDEX post_score IF NOT EXISTS
FOR (p:Post) ON (p.score);

CREATE INDEX comment_created_at IF NOT EXISTS
FOR (c:Comment) ON (c.created_at);

CREATE INDEX agent_last_active IF NOT EXISTS
FOR (a:Agent) ON (a.last_active);

CREATE INDEX submolt_subscribers IF NOT EXISTS
FOR (s:Submolt) ON (s.subscriber_count);

CREATE INDEX post_last_seen IF NOT EXISTS FOR (p:Post) ON (p.last_seen_at);
CREATE INDEX agent_last_seen IF NOT EXISTS FOR (a:Agent) ON (a.last_seen_at);
CREATE INDEX post_submolt IF NOT EXISTS FOR (p:Post) ON (p.submolt);
CREATE INDEX agent_profile_last_fetched IF NOT EXISTS FOR (a:Agent) ON (a.profile_last_fetched_at);
