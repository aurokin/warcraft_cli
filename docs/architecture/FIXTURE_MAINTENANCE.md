# Fixture maintenance

How to refresh the pinned data behind offline and live contract tests.

Fixtures come in two kinds and they are maintained differently:

- **Synthetic fixtures** are hand-written. They pin routing, URL construction, and payload key
  presence. There is no capture step and no recorder script; you edit them by hand.
- **Captured pages** are real provider HTML saved to disk. They pin parser resilience against real
  markup. They are re-captured from the live page and then trimmed.

Non-live tests run under the socket-level network guard in `tests/conftest.py`, so a fixture test
that accidentally reaches the network fails instead of silently going live. Capture work therefore
happens outside pytest, or in a test marked `live`.

## When to refresh

- A parser canary or the live matrix skips or fails with HTTP 404
- `wowhead doctor` live probes fail for a pinned expansion profile
- A schema snapshot test fails after an intentional CLI output change (update the fixture or the
  expected keys, and record the change in `CHANGELOG.md`)
- A provider redesigns the page a captured fixture came from

## Synthetic: Wowhead routing fixtures

`tests/fixtures/expansion_synthetic.json` is hand-written. It holds only what expansion routing
needs — `query`, `profiles` (each with `data_env`, `canonical_url`, `link_href`), `search_result`,
`tooltip`, `comment`, `reply_thread`. Entity-page HTML is synthesized in
`tests/article_provider_testkit.py`, not stored.

To change it:

1. Edit the JSON directly, preserving the six top-level keys and the per-profile keys above.
2. Keep one stable item (default: item 19019) across profiles so URLs stay comparable.
3. Verify:

```bash
pytest -q tests/test_expansion_synthetic_fixtures.py tests/test_wowhead_schema_snapshots.py
```

`make fixture-refresh-hints` prints the live URLs for the pinned profiles. It is a lookup aid for
checking that the hand-written URLs still resolve; it does not capture anything.

## Synthetic: Method page fixtures

`tests/fixtures/method/*.html` are small hand-written pages, one per content family
(`class_guide`, `article_guide`, `delve_guide`, `profession_guide`, `reputation_guide`,
`unsupported_index`). They exist to pin family classification and navigation extraction, so keep
them minimal: add only the markup the assertion needs.

```bash
pytest -q tests/test_method_synthetic_fixtures.py
```

## Captured: Icy Veins pages

`tests/fixtures/icy_veins/*.html` are real captured pages. Do not replace them with hand-written
stubs — their point is that the parser survives production markup.

When re-capturing, save the raw page and then trim it:

- Delete every `<style>...</style>` element.
- Delete every `<script>...</script>` element **except**:
  - `type="application/ld+json"` — the parser reads Article JSON-LD for title, author, and dates
  - any script whose body contains both `dataLayer` and `page_type` — the parser reads the GTM
    dataLayer for `page.page_type`
- Keep everything else byte-identical. Do not reserialize through BeautifulSoup.

Trimming removed ~40% of the fixture weight (4.20 MB to 2.50 MB) with no change to
`parse_guide_page()` output beyond whitespace in `article.html`, which no test asserts on. Aim well
under 100 KB per file for anything new.

```bash
pytest -q tests/test_icy_veins_cli.py tests/test_icy_veins_recorded_fixtures.py
```

## Warcraft Logs live matrix

The matrix discovers its inputs at run time: the session fixture in
`tests/test_live_command_matrix.py` picks the current raid zone from `zones`, then a Heroic or
Mythic kill in a recent public report of that zone, and anchors every case on it. Nothing ages out.
`tests/fixtures/live_matrix.py` holds only identity pins (the maintainer's guild and character)
plus discovery tuning; change those only if the guild or character moves. Run:

```bash
make test-live-matrix
```

## Wowhead parser canaries

1. Pick a replacement entity on the target expansion (same entity type).
2. Update `tests/fixtures/wowhead_canaries.py` (`entity_id`, `label`).
3. Run:

```bash
WOWHEAD_LIVE_TESTS=1 pytest -q tests/test_wowhead_parser_canaries.py
```

## After any fixture change

- Run `pytest -q -m "not live"`.
- Note the rotation in the PR or commit message; report codes and entity ids go stale over time.

## Related

- [CONTRACT_TEST_CATALOG.md](CONTRACT_TEST_CATALOG.md)
