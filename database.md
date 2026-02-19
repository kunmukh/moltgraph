# Neo4j Specific Database Queries

## Apply Schema Updates (without wiping volumes)

If you add new indexes/constraints, just re-run schema. Because statements use `IF NOT EXISTS`, this is safe.

Option A (recommended): rerun init script

```bash
docker compose run --rm crawler python -m scripts.init_db
```

Option B: apply schema directly with cypher-shell inside the Neo4j container

```bash
# Find the Neo4j container name (example output: moltbook-neo4j)
docker ps --format '{{.Names}}' | grep neo4j

# Then apply schema
docker exec -i moltbook-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" < crawler/cypher/schema.cypher
```

---

## Clear Database and Start Over

### Delete all nodes/edges (keeps schema)

Run in Neo4j Browser:

```cypher
MATCH (n) DETACH DELETE n;
```

Then re-run full crawl.

---

## Common Queries / Update Checks

### High-level counts

```cypher
MATCH (n) RETURN labels(n) AS label, count(*) AS cnt ORDER BY cnt DESC;
MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS cnt ORDER BY cnt DESC;
```

### Top submolts by number of posts observed

```cypher
MATCH (s:Submolt)<-[:IN_SUBMOLT]-(p:Post)
RETURN s.name, count(p) AS posts
ORDER BY posts DESC
LIMIT 20;
```

### Recently active agents

```cypher
MATCH (a:Agent)
WHERE a.last_active IS NOT NULL
RETURN a.name, a.karma, a.last_active
ORDER BY a.last_active DESC
LIMIT 25;
```

### Agents needing profile refresh

```cypher
MATCH (a:Agent)
WHERE a.profile_last_fetched_at IS NULL
   OR a.profile_last_fetched_at < datetime() - duration("P7D")
RETURN a.name, a.profile_last_fetched_at
ORDER BY a.profile_last_fetched_at ASC
LIMIT 200;
```

---
