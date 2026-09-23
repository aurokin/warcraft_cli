from __future__ import annotations

from bs4 import BeautifulSoup, Tag
from warcraft_content.html_sections import extract_sections


def _article(html: str) -> Tag:
    article = BeautifulSoup(html, "html.parser").find("article")
    assert isinstance(article, Tag)
    return article


def test_prose_written_straight_into_a_heading_wrapper_stays_in_its_section() -> None:
    """Both guide sites wrap headings in layout divs; text nodes beside the heading are still prose."""
    article = _article(
        """
        <article>
          <div class="section">
            <h2>Talents</h2>
            Take the left branch first.
            <p>Then spend the rest on throughput.</p>
          </div>
        </article>
        """
    )

    sections = extract_sections(article, fallback_title="Guide")

    assert [row["title"] for row in sections] == ["Talents"]
    assert sections[0]["text"] == "Take the left branch first. Then spend the rest on throughput."


def test_a_page_with_no_heading_becomes_one_section_under_the_fallback_title() -> None:
    article = _article("<article><p>Intro prose.</p></article>")

    sections = extract_sections(article, fallback_title="Mistweaver Monk")

    assert [(row["title"], row["level"], row["text"]) for row in sections] == [
        ("Mistweaver Monk", 2, "Intro prose.")
    ]
