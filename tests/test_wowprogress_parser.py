from __future__ import annotations

from wowprogress_cli.page_parser import parse_character_page, parse_guild_page, parse_pve_leaderboard_page

GUILD_HTML = """
<html>
  <body>
    <h1>“Liquid” Guild</h1>
    <strong>Horde</strong>
    <a href="/guild/us/illidan/Liquid/rating.tier34">Liberation of Undermine</a>
    <a href="/pve/us/illidan">US-Illidan</a>
    <a href="https://worldofwarcraft.com/en-us/guild/illidan/liquid">(armory)</a>
    <table>
      <tr>
        <td>
          <div>Progress: 8/8 (M)</div>
          <table>
            <tr><td>World:</td><td>1</td></tr>
            <tr><td>US:</td><td>1</td></tr>
            <tr><td>Realm:</td><td>1</td></tr>
          </table>
        </td>
        <td>
          <a href="/guild_ilvl/">Item Level: 724.51 (20-man)</a>
          <table>
            <tr><td>World:</td><td>9026</td></tr>
            <tr><td>US:</td><td>4149</td></tr>
            <tr><td>Realm:</td><td>238</td></tr>
          </table>
        </td>
      </tr>
    </table>
    <table>
      <tr>
        <th>Encounter</th>
        <th>First Kill</th>
        <th>Videos</th>
        <th>World</th>
        <th>US</th>
        <th>Realm</th>
        <th>Fastest Kill</th>
      </tr>
      <tr>
        <td><a href="/encounter/dimensius-the-all-devouring-mythic">+ M: Dimensius, the All-Devouring</a></td>
        <td>Aug 24, 2025 00:18</td>
        <td><a href="/video/1">PoV</a><a href="/video/2">PoV</a></td>
        <td>1</td>
        <td>1</td>
        <td>1</td>
        <td>15m 47s</td>
      </tr>
    </table>
  </body>
</html>
"""


CHARACTER_HTML = """
<html>
  <body>
    <div>
      <h1>Imonthegcd</h1>
      <a href="/gearscore/us/illidan/">US-Illidan</a>
      <a href="/guild/us/illidan/Liquid">Liquid</a>
      void elf mage 90
      <a href="https://worldofwarcraft.com/en-us/character/illidan/Imonthegcd">(armory)</a>
    </div>
    <div>
      Languages: English
      Looking for guild: unknown
      Raids per week: unknown
      Mythic Plus Dungeons: yes
      Specs playing: Arcane, Frost
    </div>
    <table>
      <tr>
        <td>
          <div>Item Level: 728.81</div>
          <table>
            <tr><td>West:</td><td>n/a</td></tr>
            <tr><td>realm:</td><td>6246</td></tr>
          </table>
        </td>
      </tr>
    </table>
    <table>
      <tr>
        <td>
          <div>SimDPS: 6089532.23</div>
          calculated: 3 months ago version: simc 2025-08-21 spec: Arcane
          <table>
            <tr><td>West:</td><td>1038</td></tr>
            <tr><td>US:</td><td>236</td></tr>
            <tr><td>realm:</td><td>35</td></tr>
          </table>
        </td>
      </tr>
    </table>
    <h2>PvE Score: 750000.00</h2>
    <table>
      <tr>
        <th>Manaforge Omega Bosses</th>
        <th>First Seen Kill</th>
        <th>Score</th>
      </tr>
      <tr>
        <td>Dimensius, the All-Devouring Mythic</td>
        <td>Aug 24, 2025 00:18</td>
        <td>750000.00</td>
      </tr>
    </table>
  </body>
</html>
"""


LEADERBOARD_HTML = """
<html>
  <body>
    <h1>US Mythic Progress</h1>
    <h2>Mythic Manaforge Omega</h2>
    <table>
      <tr>
        <th></th>
        <th>Guild</th>
        <th>Realm</th>
        <th>Progress</th>
      </tr>
      <tr>
        <td>1</td>
        <td><a href="/guild/us/illidan/Liquid">Liquid</a></td>
        <td><a href="/pve/us/illidan">US-Illidan</a></td>
        <td>8/8 (M)</td>
      </tr>
      <tr>
        <td>2</td>
        <td><a href="/guild/us/area-52/Instant Dollars">Instant Dollars</a></td>
        <td><a href="/pve/us/area-52">US-Area 52</a></td>
        <td>7/8 (M)</td>
      </tr>
    </table>
  </body>
</html>
"""


def test_parse_guild_page() -> None:
    payload = parse_guild_page(
        GUILD_HTML,
        url="https://www.wowprogress.com/guild/us/illidan/Liquid",
        region="us",
        realm="illidan",
        name="Liquid",
    )
    assert payload["guild"]["name"] == "Liquid"
    assert payload["progress"]["summary"] == "8/8 (M)"
    assert payload["progress"]["raid"] == "Liberation of Undermine"
    assert payload["progress"]["tier_key"] == "tier34"
    assert payload["progress"]["ranks"] == {"world": "1", "region": "1", "realm": "1"}
    assert payload["item_level"]["average"] == 724.51
    assert payload["item_level"]["ranks"] == {"world": "9026", "region": "4149", "realm": "238"}
    assert payload["encounters"]["items"][0]["encounter"] == "Dimensius, the All-Devouring"
    assert payload["encounters"]["items"][0]["video_count"] == 2
    # Encounter rows carry an additive name-only (normalized) encounter identity.
    encounter_identity = payload["encounters"]["items"][0]["encounter_identity"]
    assert encounter_identity["kind"] == "encounter_identity"
    assert encounter_identity["status"] == "normalized"
    assert encounter_identity["identity"]["encounter_id"] is None
    assert encounter_identity["identity"]["normalized_name"] == "dimensius-the-all-devouring"
    assert encounter_identity["source"] == {"provider": "wowprogress", "source": "guild_encounter_row"}
    assert payload["history_links"][0]["tier_key"] == "tier34"
    assert payload["history_links"][0]["raid"] == "Liberation of Undermine"
    assert payload["history_links"][0]["current"] is True


def test_parse_character_page() -> None:
    payload = parse_character_page(
        CHARACTER_HTML,
        url="https://www.wowprogress.com/character/us/illidan/Imonthegcd",
        region="us",
        realm="illidan",
        name="Imonthegcd",
    )
    assert payload["character"]["name"] == "Imonthegcd"
    assert payload["character"]["guild_name"] == "Liquid"
    assert payload["character"]["race"] == "Void Elf"
    assert payload["character"]["class_name"] == "Mage"
    assert payload["item_level"]["value"] == 728.81
    assert payload["item_level"]["ranks"] == {"world": None, "region": "n/a", "realm": "6246"}
    assert payload["sim_dps"]["value"] == 6089532.23
    assert payload["sim_dps"]["ranks"] == {"world": None, "region": "236", "realm": "35"}
    # spec is sourced from the SimDPS table (Fix 1: field-level coverage for the identity input).
    assert payload["sim_dps"]["spec"] == "Arcane"
    # Class is free-text parsed and spec comes from SimDPS, so identity is normalized w/o confidence.
    identity = payload["character"]["class_spec_identity"]
    assert identity["kind"] == "class_spec_identity"
    assert identity["status"] == "normalized"
    assert identity["confidence"] == "none"
    assert identity["identity"] == {"actor_class": "mage", "spec": "arcane"}
    assert identity["source"] == {"provider": "wowprogress", "source": "character_page"}
    assert payload["pve"]["score"] == 750000.0
    assert payload["pve"]["raids"][0]["raid"] == "Manaforge Omega"


def test_parse_character_summary_keeps_two_word_classes() -> None:
    payload = parse_character_page(
        "<html><body><h1>Voidy</h1><div>Void Elf Death Knight 80</div></body></html>",
        url="https://www.wowprogress.com/character/us/illidan/Voidy",
        region="us",
        realm="illidan",
        name="Voidy",
    )
    # Two-word class must not truncate to the trailing token ("Knight").
    assert payload["character"]["class_name"] == "Death Knight"
    assert payload["character"]["race"] == "Void Elf"
    assert payload["character"]["class_spec_identity"]["identity"]["actor_class"] == "deathknight"


def test_parse_character_page_identity_degrades_without_class() -> None:
    payload = parse_character_page(
        "<html><body><h1>Nobody</h1></body></html>",
        url="https://www.wowprogress.com/character/us/illidan/Nobody",
        region="us",
        realm="illidan",
        name="Nobody",
    )
    assert payload["character"]["class_name"] is None
    assert payload["sim_dps"] == {}
    identity = payload["character"]["class_spec_identity"]
    assert identity["status"] == "unknown"
    assert identity["identity"] == {"actor_class": None, "spec": None}


def test_parse_pve_leaderboard_page() -> None:
    payload = parse_pve_leaderboard_page(
        LEADERBOARD_HTML,
        url="https://www.wowprogress.com/pve/us",
        region="us",
        realm=None,
        limit=10,
    )
    assert payload["leaderboard"]["kind"] == "pve"
    assert payload["leaderboard"]["active_raid"] == "Manaforge Omega"
    assert payload["count"] == 2
    assert payload["entries"][0]["guild_name"] == "Liquid"
    assert payload["entries"][0]["rank"] == 1
    assert payload["entries"][0]["progress"] == "8/8 (M)"
