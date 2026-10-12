"""
Build the packaged per-dataset indexes (first-run fallback) for the Azure Function.

Ingests every registered source live, normalizes each, and writes one index per
dataset to code/index/<key>.pkl plus code/index/manifest.json. After deploy, the
timer trigger and POST /api/refresh keep the Blob indexes up to date.

    python build_index.py            # all datasets
    python build_index.py --public   # only public datasets (skip restricted)
"""
import argparse
import datetime
import json
import os

from sources import get_enabled_sources
from search_core import build_artifact

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "index")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true",
                    help="Only package public datasets (restricted data is not shipped).")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = {"total": 0, "datasets": []}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    for src in get_enabled_sources():
        classification = getattr(src, "classification", "public")
        if args.public and classification != "public":
            print(f"Skipping restricted dataset {src.key}")
            continue
        print(f"Loading {src.key}…")
        try:
            docs = src.normalize(src.fetch())
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: {src.key} failed to load: {exc}")
            continue
        out = os.path.join(OUT_DIR, f"{src.key}.pkl")
        n = build_artifact(docs, out)
        print(f"  {n} {src.key} records -> {out}")
        manifest["total"] += n
        manifest["datasets"].append({
            "key": src.key, "name": src.label, "classification": classification,
            "count": n, "status": "packaged", "updated_at": now,
        })

    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"Wrote manifest ({manifest['total']} records across "
          f"{len(manifest['datasets'])} datasets)")


if __name__ == "__main__":
    main()
