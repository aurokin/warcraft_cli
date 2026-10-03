from __future__ import annotations

import json

from typer.testing import CliRunner
from wowhead_cli.linked_graph import build_linked_graph_payload
from wowhead_cli.main import app

runner = CliRunner()


def test_build_linked_graph_depth_one_filters_relations() -> None:
    html = """
    <html><body>
      <a href="/npc=12056/baron-geddon">Baron</a>
      <a href="/quest=7786/thunderaan">Quest</a>
    </body></html>
    """

    def fetch_page(entity_type: str, entity_id: int):
        return html, {"canonical_url": f"https://www.wowhead.com/{entity_type}={entity_id}"}

    payload = build_linked_graph_payload(
        root_type="item",
        root_id=19019,
        root_url="https://www.wowhead.com/item=19019",
        fetch_page=fetch_page,
        depth=1,
        relation_filter={"npc"},
        node_limit=20,
        max_fetches=5,
        include_gatherer=False,
    )
    targets = {edge["to"] for edge in payload["graph"]["edges"]}
    assert "npc:12056" in targets
    assert "quest:7786" not in targets


def test_build_linked_graph_reports_the_pages_max_fetches_left_unread() -> None:
    links = "".join(f'<a href="/item={item_id}/x">x</a>' for item_id in range(1, 6))
    html = f'<html><body><a href="/item=19019/thunderfury">self</a>{links}</body></html>'

    payload = build_linked_graph_payload(
        root_type="item",
        root_id=19019,
        root_url="https://www.wowhead.com/item=19019",
        fetch_page=lambda entity_type, entity_id: (html, {}),
        depth=2,
        relation_filter=set(),
        node_limit=500,
        max_fetches=3,
        include_gatherer=False,
    )

    # The root and two of its five children were read; the other three children were not.
    assert payload["sampling"]["pages_fetched"] == 3
    assert payload["sampling"]["pages_skipped"] == 3
    assert payload["sampling"]["truncated"] is True
    assert [edge for edge in payload["graph"]["edges"] if edge["from"] == edge["to"]] == []


def test_linked_graph_command_emits_graph_payload(monkeypatch) -> None:
    html = '<html><body><a href="/npc=12056/baron-geddon">Baron</a></body></html>'

    def fake_html(self, entity_type: str, entity_id: int):
        return html

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    monkeypatch.setattr(
        "wowhead_cli.main._fetch_entity_page",
        lambda ctx, client, page_type, page_id: (html, {"canonical_url": f"https://www.wowhead.com/{page_type}={page_id}"}),
    )

    result = runner.invoke(app, ["linked-graph", "item", "19019", "--relation", "npc", "--depth", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "linked_graph"
    assert payload["data"]["graph"]["edge_count"] == 1
    assert payload["data"]["graph"]["edges"][0]["to"] == "npc:12056"


def test_linked_graph_follows_relation_tab_listviews_and_names_the_root() -> None:
    """A zone lists its NPCs and quests in Listview tabs, not body links; those used to be left out."""
    html = """
    <html><body>
      <a href="/npc=12056/baron-geddon">Baron</a>
      <script>new Listview({template: 'quest', id: 'quests', data:[{"id":7786,"name":"Thunderaan"}]});</script>
    </body></html>
    """

    payload = build_linked_graph_payload(
        root_type="zone",
        root_id=12,
        root_url="https://www.wowhead.com/zone=12",
        fetch_page=lambda entity_type, entity_id: (html, {"title": "Elwynn Forest"}),
        depth=1,
        relation_filter=set(),
        node_limit=20,
        max_fetches=5,
        include_gatherer=False,
    )

    assert payload["root"]["name"] == "Elwynn Forest"
    edges = {(edge["to"], edge["source_kind"]) for edge in payload["graph"]["edges"]}
    assert edges == {("npc:12056", "href"), ("quest:7786", "listview")}


def test_linked_graph_rejects_an_unknown_relation_type() -> None:
    """A typo such as ``npcs`` used to filter every edge away and answer ok with an empty graph."""
    result = runner.invoke(app, ["linked-graph", "item", "19019", "--relation", "npcs"])
    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_argument"
    assert error["message"].startswith("Unsupported relation 'npcs'. Expected one of: ")
