#!/usr/bin/env python3
"""Print commands and URLs to refresh recorded Wowhead expansion fixtures."""

from __future__ import annotations

import argparse
import os

from wowhead_cli.expansion_profiles import (
    build_comment_replies_url,
    build_entity_url,
    build_search_suggestions_url,
    build_tooltip_url,
    resolve_expansion,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expansion", default=os.getenv("WOWHEAD_EXPANSION", "retail"))
    parser.add_argument("--item-id", type=int, default=int(os.getenv("WOWHEAD_ITEM_ID", "19019")))
    parser.add_argument("--query", default=os.getenv("WOWHEAD_QUERY", "thunderfury"))
    args = parser.parse_args()

    profile = resolve_expansion(args.expansion)
    print(f"# Refresh hints for expansion={profile.key} item={args.item_id}")
    print(f"search_url={build_search_suggestions_url(profile)}?q={args.query}")
    print(f"tooltip_url={build_tooltip_url(profile, 'item', args.item_id)}")
    print(f"entity_url={build_entity_url(profile, 'item', args.item_id)}")
    print(f"comment_replies_url={build_comment_replies_url(profile)}")
    print("# Capture live JSON/HTML from the URLs above into tests/fixtures/expansion_recorded.json")
    print("# Then run: pytest -q tests/test_expansion_recorded_fixtures.py tests/test_wowhead_schema_snapshots.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
