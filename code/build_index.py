"""Import into Azure AI Search: python build_index.py [--public].

Uses the same refresh process as the HTTP and timer triggers. Configure
AZURE_SEARCH_ENDPOINT and authenticate with Entra; no local corpus files are written.
"""
import argparse
import json

from refresh_job import rebuild_and_upload
from sources import get_enabled_sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true", help="Import only public datasets.")
    args = parser.parse_args()
    sources = [source for source in get_enabled_sources()
               if not args.public or source.classification == "public"]
    print(json.dumps(rebuild_and_upload(sources=sources), indent=2))


if __name__ == "__main__":
    main()
