# SimulationCraft CLI (`simc`)

`simc` is the local-tool provider in this repo. It reads a local [SimulationCraft](https://github.com/simulationcraft/simc)
checkout, decodes talent builds and APLs from it, and — when the binary is built — runs short sims and
parses their JSON reports. It never talks to a web API.

## Start with a complete view

Use `describe-build` for an explicit talent build and `analysis-packet` for a bounded APL evidence
summary. Start there before choosing the narrower `priority`, `inactive-actions`, or `opener` views.
The expert inspection commands remain available when a specific question needs their evidence.
Generic `search` and `resolve` are unsupported: this local tool has no provider discovery index.

## Requirements

- a SimulationCraft source checkout for read-only analysis
- a built `simc` binary inside that checkout (`build/simc`) for `version`, `sim`, `run`, `decode-build`,
  `modify-build`, `apply-build`, `validate-talent-transport`, and the comparison commands
- `rg` (ripgrep) on `PATH` for `spec-files`, `find-action`, and `trace-action`. Without it those three
  commands fail with `missing_dependency` and `simc doctor` marks them `unavailable`.
- `git` and `cmake` for `sync`, `checkout`, and `build`. `simc build` builds only the `simc` target
  (`--target` picks another) with one job per CPU. The first configure of a checkout asks for
  `-DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF`: SimC's CMake otherwise leaves the build unoptimized and
  builds the Qt GUI, which fails to configure without Qt. Once `build/CMakeCache.txt` exists it is left
  alone, so a build type or GUI choice made by hand survives.

`simc doctor` reports which of these are present.

## Repo resolution

The checkout is resolved in this order:

1. `--repo-root` on the command line
2. the `SIMC_REPO_ROOT` environment variable
3. the explicit root saved with `simc repo --set-root <path>`
4. the managed checkout under the provider data root, created by `simc checkout`

`simc repo` prints the resolution, `simc doctor` includes it under `repo_resolution`.

## Global flags

Global flags go before the subcommand.

| Flag | Effect |
|------|--------|
| `--repo-root PATH` | Override the local SimulationCraft checkout for this invocation |
| `--pretty` | Pretty-print JSON. Default output is compact JSON |
| `--compact` | Truncate long prose strings (tooltip HTML, article text) and list each cut path in `provenance.compacted_paths`; URLs, talent/transport strings, export codes and `*command`/`*input` values stay whole. |
| `--compact-max-chars N` | Truncation length for `--compact` (40-10000) |
| `--fields a.b,c` | Keep only the listed dot paths (repeatable or comma-separated) |
| `--fields-strict` | Exit 2 with `missing_fields` when a requested path is absent |
| `--profile agent\|human` | Output presets: compact JSON, pretty JSON |

```bash
simc --repo-root ~/src/simc --pretty doctor
simc --fields data.capabilities doctor
```

## Output contract

Every command writes one JSON envelope: `ok`, `provider`, `command`, `kind`, `schema_version`, `query`,
`provenance`, `data`, and `error` on failure, and no other top-level key: the payload lives in `data`.
Failures go to stderr and use the
shared exit codes (1 generic, 2 usage, 3 auth, 4 not found, 5 network/upstream) — see
[../foundation/ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md).

`simc` has no network surface, so exit 3 and exit 5 do not occur. A missing checkout, a missing binary,
or a failed SimC run is a structured error, never a traceback.

These codes are worth knowing:

- `invalid_build` (exit 1) — SimC rejected the talent input. `error.message` is SimC's own error line
  and `error.details` carries `simc_returncode`, a 20-line `simc_output_preview` (each line clipped to
  200 characters), and `simc_binary` with the binary's build revision, the checkout HEAD, and
  `matches_checkout`. A rejected hash is never reported as a partial decode. When the binary is older
  than its checkout the message says so and names the rebuild command, because a stale binary decodes
  against older trait data; `simc doctor` reports the same mismatch.
- `missing_dependency` (exit 1) — ripgrep is not installed.
- `missing_binary` (exit 1) — `version`, `sim`, `run`, `validate-apl` or `compare-apls` found no `build/simc` in the checkout; run
  `simc build`. `missing_repo` (exit 1) — `build` or `sync` found no checkout (`build` needs the
  checkout's `CMakeLists.txt`; cmake creates the build directory itself).
- `not_found` (exit 4) — `spec-files`, `find-action`, and `trace-action` were pointed at a directory that
  is not a SimulationCraft checkout. They report this instead of returning zero hits as a success. An
  APL command also answers `not_found` for an APL file that is not there, and for a `--list` the file
  has no action list of (`error.details.available_lists` names the ones it has). `validate-apl` and
  `compare-apls` answer `not_found` for a missing harness or APL before writing anything. `sim` and `run`
  answer `not_found` for a profile path that is not a file (a directory included).
- `invalid_query` (exit 2) — `sim` or `run` was given a profile SimC found nothing to sim in (an empty
  file, or text with no actor: SimC prints `Nothing to sim!`, which `error.details.stdout_preview`
  keeps), or `sim` was told to read stdin while stdin is a terminal (pipe the profile, or pass a path
  or `--profile-text`); a build arrived without a class and spec and could not be identified
  by a command that needs one (`identify-build` instead answers `ok: true` with `identity.confidence`
  `none` or `low`, null `actor_class`/`spec` and a source note naming the specs probed),
  `decode-build` or `identify-build` was given no build at all, a build-input option was passed with
  an empty value, or the class or spec names none of SimC's specs (an unknown class, or a pair such as
  `mage holy`); the message lists the valid values. It is also the answer when `--apl-path` names a
  spec's APL (for example `monk_brewmaster.simc`) that the build does not decode as: a build is never
  described against another spec's rotation, whether the build's spec came from its hash, a talent-calc
  URL or `--actor-class`/`--spec`. The APL is checked against the spec the build is identified as, so a
  hash that contradicts its talent-calc URL path is checked as the spec the hash decodes as. Class and spec are read case-insensitively in any provider's spelling
  (the shared class/spec table): `Death Knight`, `death_knight` and `dk` mean `deathknight`, and `bm`
  means `beast_mastery`. A spec spelling that names one class fills in a missing `--actor-class`
  (`--spec bm` is hunter beast_mastery); one that names another class's spec (`--actor-class mage --spec fdk`)
  fails with `invalid_query`. Identification decodes the build once per spec in the checkout's generated
  specialization data (every playable spec, healers included) and keeps the one it decodes as; an
  `--actor-class` or `--spec` hint alone narrows the probe to that class's or spec's specs, and the
  message names what was probed (`decodes as none of the 3 deathknight specs`). When several specs
  decode it, `error.details.identity.candidates` lists them. Pass `--actor-class` and `--spec` together
  to skip the probe; a talent hash is still decoded once as that spec, and when it does not decode as
  it the identity comes back with `confidence: none` and the build commands fail with SimC's own error.
- `identify_failed` (exit 1) — identification could not probe at all because the checkout has no built
  binary, a binary that cannot be executed or that crashed (exited on a signal or with an unexpected
  code) part-way through, no generated specialization data, or no generated trait data; the message
  names which. A decode never returns the talents of a SimC run that crashed part-way.
- `unsupported_build_reference` (exit 2) — the build input is a link the CLI cannot turn into talents.
  `error.details.reference_type` names what it recognized: `wowhead_talent_calc_url` for a talent-calc
  URL with no build code, `wowhead_talent_calc_url_non_retail` for a Classic-era calculator build,
  `url` for anything else. See "Build references" below for what does decode.
- `unknown_talent` (exit 2) — an `--enable`/`--disable` value names no talent of the actor's class
  (`error.details.unknown_talents` lists them), or a `modify-build` `--add`/`--remove` value names no
  talent the build's spec can take. Talent names are tokenized the way SimC does it: a hyphen or comma
  is dropped, so "Anti-Magic Zone" is `antimagic_zone`. The underscore spellings older releases printed
  (`anti_magic_zone`, `invoke_chi_ji_the_red_crane`) are not accepted; use the display name or the
  token `decode-build` prints now.

The APL analysis commands (`apl-prune`, `apl-branch-trace`, `apl-intent`, `apl-intent-explain`,
`priority`, `opener`, `inactive-actions`, `apl-branch-compare`, `analysis-packet`, `describe-build`)
identify their build exactly as `decode-build` does, so a bad build input fails with the same code and
exit code there.

## APL analysis

- Static views use the effective action lists: `actions=` replaces a list, an empty assignment
  clears it, and `/` separates inline actions. Append assignments preserve SimC string-append
  semantics, including option continuations. Action source lines remain available as provenance.
- A relative APL path names the file under the current directory when one is there, and otherwise the
  file under the checkout (`ActionPriorityLists/default/monk_windwalker.simc`). A bare file name the
  checkout root does not hold names a spec APL in `ActionPriorityLists/default/`, so `monk_windwalker.simc`
  reads `ActionPriorityLists/default/monk_windwalker.simc`. Profile, harness and report paths are always
  relative to the current directory.
- Conditions are read with SimC's operators and precedence. `talent.X`, `talent.X.enabled`,
  `talent.X.disabled`, `talent.X.rank` and comparisons on them come from the decoded build (a rank is
  exact only when the decode reported it); `hero_tree.X` comes from the hero tree SimC activated;
  `active_enemies` and `spell_targets.*` are the target count. Everything else is runtime state. A line
  is `dead` or `eligible` only when those facts prove it, and a condition the parser cannot read in
  full is `unknown`, never `dead`.
- A line whose action is itself a talent the spec can take but the build did not (Frost's `comet_storm`
  without Comet Storm) is `dead` whatever its condition, with the reason `talent.<action>=false [action]`:
  SimC creates no action for an untaken talent. The talent universe is the checkout's
  `trait_data.inc`. A talent on a choice node whose other entry the build takes is left alone, because
  SimC can run the action as the taken entry (Brewmaster's `celestial_brew` becomes Celestial Infusion).
- `inactive-actions` (by default, `--talent-only`) and `describe-build`'s `inactive_talent_branches`
  list the dead lines that are dead because of the build: lines that are no longer dead once the build's
  talents and hero tree are treated as unknown, at the same target count. A line dead only because of the
  target count is not listed; pass `--all-dead` for every dead line.
- `apl-branch-compare` compares the start list's `run_action_list` lines and its `call_action_list` lines
  into rotation lists (helper lists excluded) in `decision_changes`. `dispatch_changed` is true when the
  guaranteed `run_action_list` target or the focus path differs, so a rotation that switches through
  `call_action_list` (Beast Mastery's `st` to `cleave`) counts; `left_dispatch`/`right_dispatch` stay the
  guaranteed `run_action_list` target, null for an APL that dispatches only through `call_action_list`.
- The focus list follows a guaranteed `run_action_list`, then a guaranteed `call_action_list` when it is
  the only live rotation dispatch and its list holds more live rows than the caller's own actions.
  Helper lists (cooldowns, trinkets, racials, variables, ...) and utility actions (auto attacks,
  interrupts, potions, trinkets, racials) do not compete. `focus_path` shows the lists it followed;
  pass `--list` to start from another list. `apl-intent`, `apl-intent-explain`, `analysis-packet`,
  `apl-branch-compare`, `priority`, `opener`, `inactive-actions` and `describe-build` use the same focus.
- Row lists cut by `--limit` say so: `priority` and `opener` report `count` (rows returned), `total`
  and `truncated`; `describe-build` reports `active_priority_total`, `active_priority_truncated` and
  `inactive_talent_branch_total`. In `inactive-actions`, in each `spec-files` category and in each
  `find-action` / `trace-action` bucket, `count` is the total before the cut, beside `truncated`
  (and the shown `items` for the latter three), so read the list's length for the rows returned.
- `spec-files QUERY` keeps the files whose name holds every word of the query, ignoring `_`, `-` and
  spaces, so `frost mage`, `mage_frost` and `death-knight` all find the APLs and `mage.cpp` finds
  `sc_mage.cpp`. Class-module sources and
  spell dumps that no file name matches fall back to a search of their contents.
- Every command's `build` block (`left`/`right` for `apl-branch-compare`) lists `enabled_talents` by
  token with `enabled_talent_count` beside it.
- `analysis-packet --first-cast-action` needs `--sim-profile` or `--profile-path` to sim; without one it
  fails with `invalid_query`. `first-cast` leaves its per-seed logs in a temp directory (each result's
  `log_path`) and removes the directory when a run fails. It times only the profile's first actor (the
  player `sim` reports), so a pet casting an action of the same name is not counted. `log-actions` names
  the `actor` behind each hit's `performed_at`, which may be a pet (`<player>_<pet>`); `--actor NAME`
  counts only that actor's lines. Before writing, `compare-apls` and `validate-apl` reject generated
  profile/report paths that collide with any harness or APL input, or with another output, as
  `invalid_query` (exit 2). This includes symlink, hardlink and case aliases.
  `validate-apl` without `--out-dir` writes its
  merged profile to a temp directory (`profile_path`) and leaves it there for you to read or delete.

## Build input flags

Commands that act on an exact build accept the same build-input group. Pass whichever form you have;
the CLI reports which one it used in `source_kind` and `identity`.

| Flag | Input |
|------|-------|
| `--apl-path PATH` | Infer actor class and spec from an APL file |
| `--profile-path PATH` | A SimC profile that contains build lines |
| `--build-file PATH` | A plain text file with `talents=` / spec lines |
| `--build-packet PATH` | A talent transport packet JSON file |
| `--build-text TEXT` | Inline build text, talent hash, or Wowhead talent-calc URL with a build code |
| `--talents TEXT` | WoW export string, Wowhead talent-calc URL with build code, or `talents=...` line |
| `--class-talents` / `--spec-talents` / `--hero-talents` | Split SimC talent strings |
| `--actor-class` / `--spec` | Explicit class and spec, e.g. `monk` and `mistweaver` |

Analysis commands additionally take `--enable NAME` and `--disable NAME` (repeatable or comma-separated)
to force talents on or off on top of the resolved build.

Passing a build-input option with an empty value is a usage error, not the same as omitting it.

A `--profile-path` or `--build-file` with several actors (a second class line or a `copy=` actor) is
read for its first actor only, the player `simc sim` reports; `source_notes` names the actors it ignored.

The class and spec an APL file name suggests (`mage_arcane.simc`) only fill what the caller left out.
With a talent hash they are a guess the hash is decoded against once: when it does not decode as that
spec the guess is ignored (`ignored apl name: the build does not decode as mage fire`) and the build is
identified by the probe. The same holds for the class and spec in a Wowhead talent-calc URL path
(`ignored talent-calc url path: ...`). When the name does not complete a SimC class/spec pair (a renamed copy such as
`mage_arcane_variant.simc`, or a `warrior_fury.simc` given with `--actor-class mage`) it is ignored and
`source_notes` says `ignored apl name: ...`; the build is then identified as if no APL were given, and an
APL view with no talents reads the file with `actor_class` and `spec` null. Such a view then knows no
class, so `--enable`/`--disable` fail with `unknown_talent` even for a real talent; pass
`--actor-class` and `--spec`.

Raw-only transport packets are not accepted as direct build input: upgrade them with
`simc validate-talent-transport --build-packet <path> --out <path>` first. Malformed packets fail with
`invalid_build_packet` on every command that reads one; a `--build-packet`, `--build-file` or
`--profile-path` that does not exist fails `not_found` (exit 4).

## Build references

`--build-text` and `--talents` accept these reference types. Anything else fails with
`unsupported_build_reference` rather than reaching SimC as if it were a talent hash.

| Reference type | Example | Decodes |
|----------------|---------|---------|
| `wow_talent_export` | `C4QAAAAAA...` | Yes, once the class and spec are known. Method and Icy Veins publish this type, and Icy Veins also converts calculator builds on PvE and PvP pages into it. Published rows may have unknown class/spec; converted rows carry both in `build_identity`. With a raw string, pass `--actor-class`/`--spec` or let identification probe every spec SimC knows. |
| `wowhead_talent_calc_url` | `https://www.wowhead.com/talent-calc/monk/mistweaver/<code>` | Yes, unaided: the path names the class and spec, which the hash is decoded against once. A path the hash contradicts is ignored and the probe identifies the build. Retail PTR and Beta calculators (`/ptr/`, `/beta/`) count as this type. |
| Wowhead `/talent-calc/blizzard/<code>` | what `modify-build` publishes as `result.wowhead_url` | Yes, as a `wow_talent_export`: the URL carries the hash but no class or spec. |
| `wowhead_talent_calc_url` with no build code | `https://www.wowhead.com/talent-calc/monk/mistweaver` | No — `unsupported_build_reference`. |
| Classic-era Wowhead calculator (`/classic/`, `/cata/`, `/mop-classic/`, `/forever/`, ...) | `https://www.wowhead.com/mop-classic/talent-calc/mage/frost/<code>` | No — `unsupported_build_reference` with `reference_type: "wowhead_talent_calc_url_non_retail"`: SimulationCraft decodes retail builds only. |
| Any other link (guide page, article, addon export site, or an Icy Veins talent calculator URL) | `https://www.icy-veins.com/wow/...` | No — `unsupported_build_reference` with `reference_type: "url"`. |

Icy Veins converted import strings preserve the selected class, spec and hero talents, but cannot
carry PvP talents. Keep the calculator URL alongside them for those selections. A conversion that
fails keeps the URL with a reason; SimC does not decode that URL directly. Classic calculator
builds can be inspected with `wowhead talent-calc`, but SimulationCraft still decodes retail builds
only. See [../wowhead/README.md](../wowhead/README.md#classic-talent-builds).

## Decoded builds

`decode-build` and `describe-build` report what SimC actually gave the player, not every line it printed:

- `hero_tree` names the hero tree SimC activated (`activating sub tree` in its debug output). A talent
  hash grants the keystones of both hero trees and SimC then disables the unselected one, so those
  talents are moved to `inactive_hero_talents` and are absent from `enabled_talents`. Keeping them there
  flips APL branches that dispatch on a hero keystone.
- A tiered node (one node whose ranks are spread over several entries) is reported as one row per entry,
  each with its own rank. SimC's decode prints a single line per tiered node holding the leftover rank,
  always `0`, so the CLI runs the build a second time with those entries set to rank `0` and reads the
  ranks back out of SimC's own overwrite log. Without those ranks the node cannot be re-serialized, and
  every `modify-build` tree swap dropped it.
- A `class_talents`/`spec_talents`/`hero_talents` entry combined with a talent hash overwrites the rank the
  hash gave that talent, as SimC does: the row carries the overwritten rank, and a talent set to `0` is
  dropped. A rank above a talent's maximum is clamped to the maximum, which is what SimC sims.
- With no hero tree selected at all (SimC's own default loadouts, a calculator build with no hero pick),
  SimC disables every hero talent the hash granted, so all of them are in `inactive_hero_talents`.
- A talent hash that decodes as no spec but whose header names one SimC knows fails `invalid_build` with
  SimC's own reason (`Node 81483 is not a choice node but has index selection`): the export predates the
  current talent tree, so passing `--actor-class` and `--spec` would not help. Export it again in game.
- Talent rows still carry `rank_known`. It is `false`, with `rank: null`, only when the read-back found
  nothing — for example when the checkout's trait data predates the node. Such a row still counts as
  enabled, and re-serializing it (a tree swap) will fail with `encode_mismatch` rather than lose it.

## Comparing builds

`compare-builds --tree` takes `class`, `spec`, or `hero`; any other value fails with `invalid_argument`
(exit 2). A `--base` or `--other` that is empty or is no build reference is a usage error. An `--other`
SimC rejects stays in `comparisons` with its `error`, and `summary` counts the `succeeded` and `failed`
comparisons; when no `--other` decodes, the command fails with the first rejection instead.

## Editing a build

`modify-build` routes each `--add`/`--remove` into the tree that owns the talent (SimC resolves talent
names per tree, so a spec talent passed as a class talent is rejected). A name or entry id must be a
talent the build's spec can take in the checkout's trait data: another class's talent, another spec's
tree, or a class-tree talent reserved for another spec (Chi Burst is Brewmaster's) fails with
`unknown_talent` (exit 2) instead of reaching SimC. A hero talent is checked the way SimC checks it:
the spec must be offered its hero tree by that tree's selection node, whatever specs the talent row
itself is tagged with (Augmentation's Chronowarden talents are tagged only for Preservation, yet
Augmentation can take them; Arcane cannot take Frostfire's). An `--add` value that
is not `name:rank` or `entry_id:rank` fails with `invalid_argument` (exit 2), and so does a
`modify-build` with no `--swap-*-tree-from`, `--add` or `--remove`. A `--swap-*-tree-from` source must
be a build of the base's own spec; another spec's build fails with `invalid_query` naming its spec.

Healer builds encode like any other. SimC refuses to simulate some healers (Mistweaver and Holy Paladin
always), so the encoder runs SimC in debug mode, which saves the profile without needing a simulated
player, and without `allow_experimental_specializations`, which made Holy Priest fail on its stale
default APL.

After re-encoding, the result is decoded again and compared per tree with the build it was supposed to
come from: the base build, or the `--swap-*-tree-from` source for a tree that was swapped. If anything
changed in the active trees that was not asked for, or a requested `--add`/`--remove` is not in the
export at the requested rank, the command fails with `encode_mismatch` instead of emitting an export:
`details.unrequested_changes` lists the former and `details.unapplied_edits` the latter (`tree`,
`talent`, `requested_rank`, `export_rank`). SimC clamps a rank above the talent's maximum and ignores
a talent it cannot place without any error, so this is the only place either shows. On success the
payload carries `result.verified: true`: every requested edit landed and nothing else changed in the
active trees. It does not mean the game will accept the export.

An `--add` on a choice node whose other entry the build takes fails with `invalid_argument` (exit 2)
before SimC runs, naming the talent to drop and its entry id: the talent hash holds one entry per choice
node, so SimC either kept the old choice or dropped it unasked. Pass the `--remove <entry id>` the
message gives to swap them; it also works when both entries share a name (Fire's two Flamestrikes).

SimC checks neither the game's per-tree point budget nor node prerequisites. When the export spends
more points in a tree than the base build did (an `--add` on a full build), `result.disclosures` says
so and warns that the game may refuse to import it.

A tree swap drops the base hash and rebuilds every tree from `entry:rank` pairs. That is lossless for a
tiered node whose per-entry ranks were read back (see "Decoded builds" above); when they were not
(`rank_known: false`), the swap fails with `encode_mismatch` naming the talent that would have been
lost rather than emitting an export without it. `--add`/`--remove` keep the base hash and are
unaffected. A build with no hero tree selected (SimC's own default talents, for one) holds only the
keystones SimC grants freely, so its hero tree is left out of the rebuild rather than spelled out,
which would make SimC select a hero tree.

`result.diff_from_base` has a fourth key, `inactive_hero`. SimC regenerates the talent hash whenever it
is handed a split talent string, and its serializer freely grants the keystone of *every* hero tree, so
the export can carry a keystone the input hash did not. Those talents are inert (the sim never activates
that tree) but the export string really does differ, so they are listed under `inactive_hero` and
`result.disclosures` explains why. An empty `disclosures` means the export matches the base build
exactly.

Validation resolves every raw row against the local SimulationCraft trait data (class, spec, hero, and
the hero-tree selection node, which is reported under tree `selection` and named after the hero tree),
re-encodes the build through the SimC binary, and decodes it back. Every entry is compared by rank,
tiered nodes included: their per-entry ranks are read back as described under "Decoded builds", and a
node whose ranks cannot be read back fails the comparison. The keystones SimC grants for the hero tree
the build did not pick are listed under `validation.round_trip.ignored_unselected_hero_entries` and
ignored. A packet stays `raw_only` with `simc_trait_resolution_incomplete` when the local checkout
predates a talent or a row repeats an entry (`duplicate_entry`) or has a negative rank
(`negative_rank`), with `multiple_hero_trees` (`hero_tree_ids`) when the rows span two hero trees,
or with `simc_round_trip_mismatch` (`expected_entries_by_tree` / `actual_entries_by_tree`) when the
decoded build differs.

## Commands

| Command | Arguments | What it returns |
|---------|-----------|-----------------|
| `analysis-packet` | APL_PATH | Bundle branch, intent, and optional first-cast timing analysis into one payload. |
| `apl-branch-compare` | APL_PATH | Compare branch dispatch between two builds or target counts on one APL. |
| `apl-branch-trace` | APL_PATH | Trace action-list dispatch for an exact build from a starting list. |
| `apl-graph` | APL_PATH | Render the action-list call graph of an APL file as Mermaid text. |
| `apl-intent` | APL_PATH | Summarize what the focus action list is trying to do for an exact build. |
| `apl-intent-explain` | APL_PATH | Explain the focus list as setup, helper, burst, and priority buckets. |
| `apl-lists` | APL_PATH | List the action lists in an APL file with their entries. |
| `apl-prune` | APL_PATH | Classify APL entries as eligible, dead, or unknown for an exact build. |
| `apl-talents` | APL_PATH | List the talents an APL file references and the most common actions. |
| `build` | - | Build the local SimulationCraft binary with cmake. |
| `build-harness` | - | Write a harness profile for the resolved build with no APL actions. |
| `checkout` | - | Clone or update the managed SimulationCraft checkout. |
| `compare-apls` | HARNESS_PATH | Sim a base APL against labelled variants and rank them by DPS. |
| `compare-builds` | - | Diff a base talent build against one or more other builds, per tree. |
| `decode-build` | - | Decode a talent build into per-tree talents using the local SimC binary. |
| `describe-build` | - | Describe a build end to end: talents, priority, and single-target versus AoE differences. |
| `doctor` | - | Report SimulationCraft repo readiness, binary version, and per-command capabilities. |
| `find-action` | ACTION | Find an action, buff, or token across APLs, class modules, and spell dumps. |
| `first-cast` | PROFILE_PATH ACTION | Time the first cast of an action across several short sims. |
| `identify-build` | - | Resolve class/spec identity for a build without decoding its talents. |
| `inactive-actions` | APL_PATH | List the APL actions an exact build cannot use. |
| `inspect` | [TARGET] | Describe the repo, or one file or directory, including any build lines a file carries. |
| `log-actions` | LOG_PATH ACTIONS | Report when actions were first scheduled and performed in a SimC combat log. |
| `modify-build` | - | Apply talent swaps, additions, and removals to a build and re-encode it. |
| `opener` | APL_PATH | Preview the early priority for an exact build, flagging runtime-only conditions. |
| `priority` | APL_PATH | Return the static active priority for an exact build, excluding inactive talent branches. |
| `repo` | - | Show or change which local SimulationCraft checkout the CLI uses. |
| `resolve` | QUERY | Reject unsupported free-text resolution; use `identify-build` for an explicit build. |
| `run` | PROFILE_PATH | Run a profile through the local SimC binary with raw SimC arguments. |
| `search` | QUERY | Reject unsupported free-text search; use typed local inspection commands. |
| `sim` | [PROFILE_PATH] | Run a profile through the local SimC binary and summarize the JSON report. |
| `spec-files` | [QUERY] | List APL and class-module files in the checkout, optionally narrowed by the words of a query. |
| `sync` | - | Pull the latest SimulationCraft sources into the local checkout. |
| `trace-action` | APL_PATH ACTION | Trace one action through an APL file and the surrounding source. |
| `validate-apl` | HARNESS_PATH APL_PATH | Append an APL to a harness profile and check that SimC parses the result. |
| `validate-talent-transport` | - | Round-trip raw talent rows through SimulationCraft and report the validated transport forms. |
| `variant-report` | REPORT_PATH | Summarize a saved compare-apls JSON report. |
| `verify-clean` | - | Report whether the checkout and built binary are unmodified. |
| `version` | - | Report the version reported by the local SimC binary. |

`find-action` and `trace-action` search the spell dumps case-insensitively and read `_` as a space
there, so a token (`rising_sun_kick`) and a display name (`"Rising Sun Kick"`) both find
`Name : Rising Sun Kick`. `--class` takes any spelling of a class (`deathknight`, `"Death Knight"`)
or shorthand (`dk`, `dh`) and keeps that class's own module files and spell dumps; an unknown class fails with `invalid_query`
and lists the valid ones. `inspect` on a file that is not text fails with `invalid_query`.

`apl-branch-compare` takes the right-hand build only from the `--right-*` options once any right-hand
build source is given (`--right-profile-path`, `--right-build-file`, `--right-build-text`,
`--right-talents`, or a `--right-*-talents` split string); nothing of the left build carries over. Without
one, the right side is the left build again with any `--right-actor-class`/`--right-spec` and
`--right-enable`/`--right-disable` layered on, which compares target counts or talent overrides.

`search` and `resolve` fail with `unsupported_operation` (exit 2). SimC has no free-text discovery
index; use local inspection commands or `identify-build` for an explicit build.

Flags, defaults, and value ranges are in [reference/simc.md](../reference/simc.md) and
`simc <command> --help`.

## Sim presets

`simc sim` is the preferred consumer run path. It uses fixed presets instead of leaving iteration
counts implicit, and always returns run settings, runtime timing, and core metrics:

- `--preset quick` (default): 1000 iterations, `target_error=0`, `max_time=300`
- `--preset high-accuracy`: 5000 iterations, `target_error=0`, `max_time=300`

The preset fills only what the profile leaves unset: a profile with its own `iterations` or
`target_error` (a Raidbots input sets both) runs with them, and one with its own `max_time` keeps it,
and `disclosures` says which profile settings were kept. Settings are read per whitespace-separated
option as SimC reads them, so `iterations=50 max_time=60` on one line sets both. A flag always wins: `--iterations` sets the
count with `target_error=0`, `--max-time` the fight length, and `disclosures` names each profile
setting a flag replaced (`iterations=500 -> 50`). The other settings (`--threads`, `--targets`,
`--fight-style`, `--vary-combat-length`) apply only when given. `metrics.dps_error` is SimC's DPS error, the half-width of
the confidence interval around mean DPS (`dps.mean_std_dev * confidence_estimator` in the json2 report),
and `run_settings.target_error_percent` is that error as a percentage of mean DPS. Default to `quick`
for consumer work and only reach for `high-accuracy` when the user asks for it. Do not hard-code thread
counts in guidance; inspect the machine first.

`run_settings.iterations_requested` is the count the CLI asked SimC for, or the profile's own `iterations`
when it was kept. `iterations_completed` is the
number of sampled iterations, which for a fixed iteration count is one less than requested: SimC
discards each run's first iteration, whatever the thread count. A profile with several actors reports the first
in `player`/`metrics`, the rest in `other_actors` (each with `player` and `metrics`), and the total in
`actor_count`. A profile that defines profilesets (a Top Gear or Droptimizer input) reports their
ranked rows in `profilesets` (`metric`, `result_count`, `results` best mean first); each row carries
SimC's `mean_error` (the confidence-interval half-width around its `mean`, so two rows closer than that
are a tie), `mean_stddev` and `iterations`. Otherwise `profilesets` is null. A profile that sets `calculate_scale_factors=1` (optionally with
`scale_only=intellect,crit_rating,haste_rating,mastery_rating,versatility_rating`) reports SimC's stat
weights for the first actor in `scale_factors`: `factors` is DPS per point of each stat and `deltas` the
stat amount each was measured with. A stat left out of `scale_only` reads 0 there, which means not
measured, not worthless. Without it `scale_factors` is null. Scale factors run one extra sim per stat, so
expect the run to take that much longer. An unknown `--preset` or an empty profile fails with
`invalid_query` (exit 2).

`disclosures` lists what limits the result. A `build-harness` profile sims the build's own `talents=`
(it no longer sets `load_default_talents=1`, which made SimC apply the spec's default talents instead).
A harness written with no talents keeps `load_default_talents=1` and sims the spec's default talents.
`build-harness`, `sim` and `compare-apls` say so in `disclosures` whenever a profile sets
`load_default_talents=1`, which also flags a harness written by an earlier version: regenerate it with
`build-harness` before comparing.
A profile that sets `load_default_gear=1`, as every `build-harness` profile does (and `build-harness` says so in its own `disclosures`), wears SimC's low
item level default gear, so its absolute DPS is far below a geared character's; only comparisons on the
same profile are meaningful. To simulate a guide build on supplied gear, use a standalone,
single-actor profile with an explicit class and spec (for example a matching checkout profile or a
character export), then apply the guide's packet:

```bash
simc apply-build ./geared-mage-frost.simc --build-packet ./guide-build.json --out ./guide-variant.simc
simc sim ./guide-variant.simc
```

`--build-text` accepts explicit SimC build text instead of a packet; supply exactly one. The build
must decode successfully and match the profile's class/spec. The output replaces all old talent
forms, append overrides, and default/all-talent loading, while retaining supplied gear and other
simulation settings, including independently scoped expansion talents (`omnium_talents`).
The command does not infer gear or simulate the result. It rejects multiple actors, `input`, `copy`,
and `profileset` directives because their effective profile cannot be established from one file.
Player imports (`armory`, `guild`, `local_json`, `player_simplified`) and explicit pet/guardian/active
actor controls must first be expanded into a standalone player profile; enemy settings are retained.
The destination must differ from the source, including symlink or hardlink aliases. Existing output
requires `--overwrite`; the source profile is never changed.

`validate-apl` and each `compare-apls` validation list SimC's `Warning:` lines in `warnings`. SimC
exits 0 after ignoring an unknown option such as a mistyped condition (`frostbolt,iff=1` runs Frostbolt
unconditionally), so a warning makes `valid` false, and `compare-apls` stops on it as on a failed
validation. No stock APL produces one.

`simc run` passes raw SimC arguments through. Its `result_lines` holds the `Player:`, `Target:` and
`Add:` headers of SimC's text report, each followed by that actor's `DPS=`/`HPS=`/`DTPS=`/`TMI=` lines,
so a target's `DTPS=` line sits under its `Target:` header; `stdout_preview` and `stderr_preview` (also on `sync`, `build`,
`validate-apl`, the `compare-apls` validations, and the error details of a failed `sim`) are the last
20 lines, with `stdout_truncated`/`stderr_truncated` set when earlier lines were cut.

## Comparison workflow

Draft and compare APL variants without touching the upstream checkout:

```bash
simc build-harness --talents "<export>" --out ./tmp/harness.simc
simc validate-apl ./tmp/harness.simc ./tmp/variant.simc
simc compare-apls ./tmp/harness.simc --base-apl ./tmp/base.simc --variant "variant=./tmp/variant.simc" --report-out ./tmp/report.json
simc variant-report ./tmp/report.json
simc verify-clean --hash-binary
```

`dps` and `fight_length` are means over every iteration, and `dps_error` is SimC's confidence-interval
half-width around that mean. Each label (`--base-label` and every `--variant` label) names the
`<label>.simc` and `<label>.json` files written for it, so labels must be unique plain file names;
anything else fails with `invalid_query`. `action_counts` are SimC's mean executes per iteration of each
of the player's actions over every iteration (`players[0].stats[].num_executes.mean`, the numbers in
SimC's own ability table, so procs and triggered spells are included and pets are not), and
`action_cpm` divides them by the mean `fight_length`; `top_action_deltas` compares those CPMs.
`sampling.note` says the same.

## Tests that need the binary

`tests/test_simc_real_binary.py` drives the real SimC binary over its checkout's own stock MID1
profiles, plus a Mistweaver and a Holy Priest build for the healer encode path. It runs only when
`WARCRAFT_SIMC_TESTS_REPO` names a SimulationCraft checkout
(`WARCRAFT_SIMC_TESTS_REPO=~/code/simc pytest tests/test_simc_real_binary.py`); the configured or
managed checkout is never read. Without the variable it skips with `REAL-BINARY TEST SKIPPED` in the
skip reason, so it proves nothing on CI; with it but no built binary it fails. The same logic is
covered everywhere else by `tests/test_simc_build_input.py` and `tests/test_simc_cli.py`, which replay
captured SimC output.

## Analysis boundary

The analysis commands stay inside what the local source tree and runtime samples prove. They summarize
structure, branches, and sampled timings, and they name the next command worth running. They do not
synthesize authoritative build advice beyond that evidence.

## Source links

- `https://github.com/simulationcraft/simc`
- [Error and envelope contract](../foundation/ERROR_CONTRACT.md)
- [Roadmap](../ROADMAP.md)
