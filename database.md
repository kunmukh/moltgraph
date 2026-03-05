# Neo4j Specific Database Queries

## Check Database Size

```shell
$ docker exec moltbook-neo4j sh -c 'du -sh /data /data/databases /data/transactions 2>/dev/null'
1.1G    /data

$ docker exec moltbook-neo4j sh -c 'du -sh /data/databases/neo4j /data/databases/system /data/transactions/neo4j /data/transactions/system 2>/dev/null'
312M    /data/databases/neo4j
1.2M    /data/databases/system
514M    /data/transactions/neo4j
258M    /data/transactions/system
```

## Download Database dump

```shell
# 1) Get the exact Neo4j version running in your container
VER=$(docker exec moltbook-neo4j neo4j --version | awk '{print $NF}')

# 2) Make a local folder for dump files
mkdir -p "$HOME/neo4j_backups"

# 3) Stop the running Neo4j container (required for Community/offline dump)
docker stop moltbook-neo4j

# 4) Dump the main database
docker run --rm -it \
  -v moltbook_neo4j_data:/data \
  -v "$HOME/neo4j_backups":/backups \
  neo4j/neo4j-admin:$VER \
  neo4j-admin database dump neo4j --to-path=/backups --overwrite-destination=true

# 5) Dump the system database too
docker run --rm -it \
  -v moltbook_neo4j_data:/data \
  -v "$HOME/neo4j_backups":/backups \
  neo4j/neo4j-admin:$VER \
  neo4j-admin database dump system --to-path=/backups --overwrite-destination=true

# 6) Start Neo4j again
docker start moltbook-neo4j
```

- Backup location: `$HOME/neo4j_backups/neo4j.dump` and `$HOME/neo4j_backups/system.dump`

## Apply Schema Updates (without wiping volumes)

To add new indexes/constraints, just re-run schema. Because statements use `IF NOT EXISTS`, this is safe.

Option A (recommended): rerun init script

```bash
docker compose run --rm crawler python -m scripts.init_db
```

Option B: apply schema directly with cypher-shell inside the Neo4j container

```bash
# Find the Neo4j container name
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
