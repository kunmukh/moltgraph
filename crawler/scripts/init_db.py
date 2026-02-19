import os
from neo4j import GraphDatabase

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "cypher", "schema.cypher")

def main():
    uri = os.environ["NEO4J_URI"]
    user = os.environ["NEO4J_USER"]
    pwd = os.environ["NEO4J_PASSWORD"]

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    with driver.session() as session:
        # Split on semicolons carefully
        statements = [s.strip() for s in schema.split(";") if s.strip()]
        for stmt in statements:
            session.run(stmt)
    driver.close()
    print("✅ Neo4j schema applied.")

if __name__ == "__main__":
    main()
