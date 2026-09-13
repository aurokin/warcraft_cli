#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_OUT_DIR = Path("research/warcraftlogs-docs")
DEFAULT_SITE = "retail"
SITE_BASE_URLS = {
    "retail": "https://www.warcraftlogs.com",
    "classic": "https://classic.warcraftlogs.com",
    "fresh": "https://fresh.warcraftlogs.com",
}


def _run_playwright(args: list[str]) -> str:
    result = subprocess.run(
        ["playwright-cli", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _extract_result_json(output: str) -> object:
    match = re.search(r"### Result\s*(.*?)\s*### Ran Playwright code", output, re.DOTALL)
    if not match:
        raise RuntimeError(f"Could not parse Playwright output:\n{output[:1000]}")
    payload = match.group(1).strip()
    return json.loads(payload)


def _slug_for_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path in ("", "/"):
        return "index"
    path = parsed.path.strip("/")
    return path.replace("/", "__")


def _write_page_dump(base_dir: Path, bucket: str, data: dict[str, object]) -> None:
    bucket_dir = base_dir / bucket
    bucket_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug_for_url(str(data["url"]))
    json_path = bucket_dir / f"{slug}.json"
    txt_path = bucket_dir / f"{slug}.txt"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
    txt_path.write_text(str(data.get("text", "")) + "\n")


def _landing_url(base_url: str) -> str:
    return f"{base_url}/api/docs"


def _graphql_index_url(base_url: str) -> str:
    return f"{base_url}/v2-api-docs/warcraft/"


def _open_session(landing_url: str) -> None:
    _run_playwright(
        [
            "open",
            landing_url,
            "--browser",
            "chrome",
            "--headed",
            "--persistent",
        ]
    )


def _goto(url: str) -> None:
    _run_playwright(["goto", url])


def _page_dump() -> dict[str, object]:
    output = _run_playwright(
        [
            "eval",
            "() => ({title: document.title, url: location.href, text: document.body.innerText, "
            "html_lang: document.documentElement.lang || null})",
        ]
    )
    payload = _extract_result_json(output)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected page payload: {payload!r}")
    return payload


def _graphql_links(base_url: str) -> list[str]:
    graphql_prefix = f"{base_url}/v2-api-docs/warcraft/"
    output = _run_playwright(
        [
            "eval",
            "() => Array.from(document.querySelectorAll(\"a[href]\"), a => a.href)"
            f".filter(h => h.startsWith(\"{graphql_prefix}\"))"
            ".filter((h, i, arr) => arr.indexOf(h) === i)",
        ]
    )
    payload = _extract_result_json(output)
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected links payload: {payload!r}")
    return [str(item) for item in payload]


def dump_docs(out_dir: Path, *, base_url: str, site: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    landing_url = _landing_url(base_url)
    graphql_index_url = _graphql_index_url(base_url)

    _open_session(landing_url)

    _goto(landing_url)
    landing = _page_dump()
    _write_page_dump(out_dir, "landing", landing)

    _goto(graphql_index_url)
    graphql_index = _page_dump()
    _write_page_dump(out_dir, "graphql-warcraft", graphql_index)

    links = _graphql_links(base_url)
    manifest = {
        "site": site,
        "base_url": base_url,
        "landing_url": landing_url,
        "graphql_index_url": graphql_index_url,
        "graphql_page_count": len(links),
        "graphql_links": links,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n")

    for url in links:
        _goto(url)
        data = _page_dump()
        _write_page_dump(out_dir, "graphql-warcraft", data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump rendered Warcraft Logs docs via playwright-cli.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory for the docs dump.")
    parser.add_argument(
        "--site",
        choices=sorted(SITE_BASE_URLS),
        default=DEFAULT_SITE,
        help="Which Warcraft Logs site variant to dump.",
    )
    parser.add_argument("--base-url", help="Optional explicit base URL override for the site variant.")
    args = parser.parse_args()
    site = str(args.site)
    base_url = str(args.base_url or SITE_BASE_URLS[site]).rstrip("/")
    dump_docs(Path(args.out_dir).expanduser().resolve(), base_url=base_url, site=site)


if __name__ == "__main__":
    main()
