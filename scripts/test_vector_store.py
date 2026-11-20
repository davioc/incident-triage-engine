"""
Manual smoke test for the runbook vector store: ingests docs/runbooks/*.md and
runs similarity queries. Run as `python scripts/test_vector_store.py`.
"""

from src.vector_store import get_vector_store

TEST_QUERIES = [
    "Database Connection Timeout on pool acquisition",
    "Redis cluster unreachable host=10.0.4.12",
    "Rate limiter triggered user_id=89231",
]


def main() -> None:
    store = get_vector_store()

    print("--- 1. Ingesting Runbooks ---")
    count = store.ingest_runbooks()
    print(f"Documents in vector DB: {store.collection.count()}\n")

    if count == 0:
        print("No runbooks found; nothing to query.")
        return

    print("--- 2. Testing Similarity Queries ---")
    for query in TEST_QUERIES:
        print(f"Query: '{query}'")
        results = store.query_runbooks(query, n_results=1)
        if results:
            match = results[0]
            print(f"Top Match: {match['metadata']['title']} (Source: {match['metadata']['source']})")
            print(f"Cosine distance (lower = closer): {match['distance']:.4f}")
            print(f"Preview: {match['content'][:150].strip()}...")
        else:
            print("No matches found.")


if __name__ == "__main__":
    main()
