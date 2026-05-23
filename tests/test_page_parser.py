from __future__ import annotations

from wowhead_cli.page_parser import (
    canonical_comment_url,
    clean_markup_text,
    extract_comments_dataset,
    extract_gatherer_entities,
    extract_guide_rating,
    extract_guide_section_chunks,
    extract_guide_sections,
    extract_json_ld,
    extract_json_script,
    extract_linked_entities_from_href,
    extract_markup_by_target,
    extract_markup_urls,
    normalize_comments,
    parse_page_metadata,
)


def test_parse_page_metadata() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="Thunderfury">
      <meta name="description" content="Legendary sword">
      <link rel="canonical" href="https://www.wowhead.com/item=19019/thunderfury">
    </head></html>
    """
    metadata = parse_page_metadata(html, fallback_url="https://www.wowhead.com/item=19019")
    assert metadata["canonical_url"] == "https://www.wowhead.com/item=19019/thunderfury"
    assert metadata["title"] == "Thunderfury"
    assert metadata["description"] == "Legendary sword"


def test_extract_linked_entities_from_href() -> None:
    html = """
    <a href="/item=19019/thunderfury">Thunderfury</a>
    <a href="https://www.wowhead.com/cata/spell=21992/thunderfury"><span>Spell</span></a>
    <a href="/fr/npc=12056/baron-geddon">Baron</a>
    <a href="https://example.com/item=1">Ignore</a>
    """
    records = extract_linked_entities_from_href(
        html, source_url="https://www.wowhead.com/item=19019/thunderfury"
    )
    pairs = {(row["entity_type"], row["id"]) for row in records}
    assert ("item", 19019) in pairs
    assert ("spell", 21992) in pairs
    assert ("npc", 12056) in pairs
    assert len(records) == 3
    by_pair = {(row["entity_type"], row["id"]): row for row in records}
    assert by_pair[("item", 19019)]["name"] == "Thunderfury"
    assert by_pair[("spell", 21992)]["name"] == "Spell"
    assert by_pair[("npc", 12056)]["name"] == "Baron"


def test_extract_linked_entities_preserves_expansion_prefix_in_url() -> None:
    html = '<a href="/wotlk/item=19019/thunderfury">Thunderfury</a>'
    records = extract_linked_entities_from_href(html, source_url="https://www.wowhead.com/wotlk/item=19019")
    assert records[0]["url"] == "https://www.wowhead.com/wotlk/item=19019"


def test_extract_gatherer_entities() -> None:
    html = (
        'WH.Gatherer.addData(1, 1, {"12056":{"name_enus":"Baron Geddon"}});'
        'WH.Gatherer.addData(3, 1, {"19019":{"name_enus":"Thunderfury"}});'
    )
    records = extract_gatherer_entities(html, source_url="https://www.wowhead.com/item=19019")
    pairs = {(row["entity_type"], row["id"]) for row in records}
    assert ("npc", 12056) in pairs
    assert ("item", 19019) in pairs
    by_pair = {(row["entity_type"], row["id"]): row for row in records}
    assert by_pair[("npc", 12056)]["url"] == "https://www.wowhead.com/npc=12056"
    assert by_pair[("npc", 12056)]["citation_url"] == "https://www.wowhead.com/npc=12056"


def test_extract_gatherer_entities_preserves_expansion_prefix_in_url() -> None:
    html = 'WH.Gatherer.addData(3, 1, {"19019":{"name_enus":"Thunderfury"}});'
    records = extract_gatherer_entities(html, source_url="https://www.wowhead.com/wotlk/item=19019")
    assert records[0]["url"] == "https://www.wowhead.com/wotlk/item=19019"
    assert records[0]["citation_url"] == "https://www.wowhead.com/wotlk/item=19019"


def test_extract_and_normalize_comments() -> None:
    html = """
    <script>
      var lv_comments0 = [{"id": 7, "number": 0, "user": "A", "body": "One", "date": "2024-01-01T00:00:00-06:00", "rating": 4, "nreplies": 1, "replies": [{"id": 8, "commentid": 7, "username": "B", "body": "Two", "creationdate": "2024-01-02T00:00:00-06:00", "rating": 2}]}];
    </script>
    """
    comments = extract_comments_dataset(html)
    normalized = normalize_comments(
        comments,
        page_url="https://www.wowhead.com/item=19019/thunderfury",
        include_replies=True,
    )
    assert len(normalized) == 1
    assert normalized[0]["citation_url"] == canonical_comment_url(
        "https://www.wowhead.com/item=19019/thunderfury", 7
    )
    assert normalized[0]["replies"][0]["id"] == 8


def test_extract_json_script_and_markup_targets() -> None:
    html = """
    <script type="application/json" id="data.nav">"[url=guide/classes/mage]Mage[/url]"</script>
    <script type="application/json" id="data.body">"[h2]Overview[/h2]\\r\\nBody text"</script>
    <script>
      WH.markup.printHtml(WH.getPageData("nav"), "interior-sidebar-related-markup");
      WH.markup.printHtml(WH.getPageData("body"), "guide-body", {"allow":30});
    </script>
    """
    assert extract_json_script(html, "data.nav") == "[url=guide/classes/mage]Mage[/url]"
    assert extract_markup_by_target(html, target="guide-body") == "[h2]Overview[/h2]\r\nBody text"
    assert extract_markup_by_target(html, target="interior-sidebar-related-markup") == "[url=guide/classes/mage]Mage[/url]"


def test_extract_markup_target_from_inline_string() -> None:
    html = """
    <script>
      WH.markup.printHtml("[h2]Overview[/h2]\\r\\nBody text", "guide-body", {"allow":30});
    </script>
    """
    assert extract_markup_by_target(html, target="guide-body") == "[h2]Overview[/h2]\r\nBody text"


def test_extract_guide_sections_urls_rating_and_json_ld() -> None:
    markup = "[h2][color=c6]Overview[/color][/h2][h3]Rotation[/h3][url=guide/classes/mage]Mage[/url][url guide=3143]Guide[/url]"
    html = """
    <script type="application/ld+json">{"@type":"Article","headline":"Guide"}</script>
    <script>$('#guiderating').append(GetStars(4.61597, false, 0, 3143));</script>
    <span id="guiderating-votes">70</span>
    """
    sections = extract_guide_sections(markup)
    links = extract_markup_urls(markup, source_url="https://www.wowhead.com/guide=1")
    rating = extract_guide_rating(html)
    json_ld = extract_json_ld(html)
    assert sections == [
        {"level": 2, "title": "Overview"},
        {"level": 3, "title": "Rotation"},
    ]
    assert links[0]["url"] == "https://www.wowhead.com/guide/classes/mage"
    assert links[1]["url"] == "https://www.wowhead.com/guide=3143"
    assert rating == {"score": 4.61597, "votes": 70}
    assert json_ld == {"@type": "Article", "headline": "Guide"}
    assert clean_markup_text("[spell=49020] Obliterate") == "Obliterate"


def test_extract_guide_section_chunks() -> None:
    markup = "[h2]Overview[/h2]Welcome to the guide.[h3]Rotation[/h3][spell=49020] Use Obliterate."
    chunks = extract_guide_section_chunks(markup)
    assert chunks == [
        {
            "ordinal": 1,
            "level": 2,
            "title": "Overview",
            "content_raw": "Welcome to the guide.",
            "content_text": "Welcome to the guide.",
        },
        {
            "ordinal": 2,
            "level": 3,
            "title": "Rotation",
            "content_raw": "[spell=49020] Use Obliterate.",
            "content_text": "Use Obliterate.",
        },
    ]
