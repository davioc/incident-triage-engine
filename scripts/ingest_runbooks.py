"""
Ingest docs/runbooks/*.md into ChromaDB and run similarity queries. The served app
turns retrieval on, so this is what makes runbook grounding work; the container runs
it at startup (upsert is idempotent). Run as `python scripts/ingest_runbooks.py`.
"""

import os
import sys

# The project root must be importable for 'src.*' to resolve, whether this is run
# from a shell or as the container's startup command. Imports follow deliberately.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.vector_store import get_vector_store  # noqa: E402

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
            title = match["metadata"]["title"]
            source = match["metadata"]["source"]
            print(f"Top Match: {title} (Source: {source})")
            print(f"Cosine distance (lower = closer): {match['distance']:.4f}")
            print(f"Preview: {match['content'][:150].strip()}...")
        else:
            print("No matches found.")


if __name__ == "__main__":
    main()
