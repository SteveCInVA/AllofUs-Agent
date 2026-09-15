"""
Build the portable search artifact (corpus.pkl) for the Azure Function.

Ingests every registered source live, normalizes them, and writes corpus.pkl next
to function_app.py so the package ships with a first-run snapshot. After deploy,
the timer trigger and POST /api/refresh keep the blob snapshot up to date.

    python build_index.py
"""
import os

from sources import get_enabled_sources
from search_core import build_artifact

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    all_docs = []
    for src in get_enabled_sources():
        print(f"Loading {src.key}…")
        try:
            docs = src.normalize(src.fetch())
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: {src.key} failed to load: {exc}")
            continue
        print(f"  {len(docs)} {src.key} records")
        all_docs += docs
    out = os.path.join(HERE, "corpus.pkl")
    n = build_artifact(all_docs, out)
    size = os.path.getsize(out) / 1024 / 1024
    print(f"Wrote {out} - {n} records, {size:.1f} MB")


if __name__ == "__main__":
    main()
