from __future__ import annotations

from bs4 import BeautifulSoup
from warcraft_content.guide_page import extract_linked_entities

SOURCE_URL = "https://www.example.com/guides/frost-mage"


def test_a_spell_first_linked_by_its_icon_takes_its_name_and_identity_from_the_later_text_link() -> None:
    article = BeautifulSoup(
        "<article>"
        '<a href="https://www.wowhead.com/spell=118"><img alt=""></a>'
        '<a href="https://www.wowhead.com/spell=118/polymorph">Polymorph</a>'
        '<a href="https://www.example.com/guides/fire-mage">Fire Mage</a>'
        "</article>",
        "html.parser",
    )

    rows = extract_linked_entities(article, source_url=SOURCE_URL, provider="method")

    assert [(row["type"], row["id"], row["name"], row["url"]) for row in rows] == [
        ("spell", 118, "Polymorph", "https://www.wowhead.com/spell=118")
    ]
    assert rows[0]["ability_identity"]["identity"]["normalized_name"] == "polymorph"


def test_site_page_turns_links_to_the_provider_own_pages_into_page_rows() -> None:
    article = BeautifulSoup('<article><a href="/guides/fire-mage">Fire Mage</a></article>', "html.parser")

    rows = extract_linked_entities(
        article,
        source_url=SOURCE_URL,
        provider="icy-veins",
        site_page=lambda url: url.rsplit("/", 1)[-1] if "/guides/" in url else None,
    )

    assert [(row["type"], row["id"], row["name"]) for row in rows] == [("page", "fire-mage", "Fire Mage")]
    assert "ability_identity" not in rows[0]
