"""
Build the portable search artifact (corpus.pkl) for the Azure Function.

Fetches the two public All of Us JSON feeds live, normalizes them, and writes
corpus.pkl next to function_app.py. Run this once locally before publishing so
the package ships with a first-run snapshot; after deploy, the timer trigger and
POST /api/refresh keep the blob snapshot up to date automatically.

    python build_index.py
"""
import os

import feeds
from search_core import build_artifact

HERE = os.path.dirname(os.path.abspath(__file__))
# Optional local fallback if the live endpoint hiccups (HTTP 500 / rate limit).
LOCAL_DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data"))


def main():
    print("Loading publications + projects...")
    pubs, projs = feeds.fetch_all(LOCAL_DATA)
    print(f"  {len(pubs)} publications, {len(projs)} projects")
    out = os.path.join(HERE, "corpus.pkl")
    n = build_artifact(pubs, projs, out)
    size = os.path.getsize(out) / 1024 / 1024
    print(f"Wrote {out} - {n} records, {size:.1f} MB")


if __name__ == "__main__":
    main()
