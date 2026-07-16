# History Mover

[![HACS: custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Tests](https://github.com/dmatscheko/history_mover/actions/workflows/test.yml/badge.svg)](https://github.com/dmatscheko/history_mover/actions/workflows/test.yml)
[![Validate](https://github.com/dmatscheko/history_mover/actions/workflows/validate.yml/badge.svg)](https://github.com/dmatscheko/history_mover/actions/workflows/validate.yml)

> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!
> UNTESTED - DO NOT USE!

A Home Assistant integration that **moves recorder history — states *and* long-term
statistics — from one entity onto another**, so a replacement sensor can adopt the
long history of the one it supersedes and keep recording into it.

## Why this exists

Modern Home Assistant already does the easy case for you: when you rename a
**registered** entity to a **free** entity id through the UI, the recorder moves
its history along ([`recorder/entity_registry.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/recorder/entity_registry.py)).
You do **not** need this integration for that.

History Mover fills the gaps the built-in rename leaves open:

- **Replace an occupied target.** You installed a better integration for, say, PV
  power or outdoor temperature, and want the *old* integration's long history to
  become the *new* entity's history. The built-in rename refuses this ("*the new
  entity_id is already in use*"). History Mover discards the new entity's short
  history and moves the old one onto it.
- **Rescue orphaned history.** The integration that created an entity is gone, so
  there is no registry entry left to rename — but its history is still in the
  database. History Mover can re-home it by id.
- **Bulk / migration.** Move hundreds of entities in one operation (e.g. migrating
  a whole device from one integration to another) by prefix or by an explicit list.

After the move, the live target **continues recording into the adopted history**,
usually with no restart.

> [!WARNING]
> History Mover edits the recorder database directly. Moving onto an occupied
> target **permanently discards that target's existing history** of the same kind
> — that is the intended "replace" behaviour. Use **Dry run** first, and back up
> your database (or take a HA backup) before a large migration.

## Installation

**HACS (recommended)**

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/dmatscheko/history_mover`, category *Integration*.
2. Install **History Mover**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → History Mover*.

**Manual**

Copy `custom_components/history_mover` into your Home Assistant `config/custom_components/` directory and restart.

## Using it

### Guided UI

On the **History Mover** card, click **Configure**:

- **Single entity** — enter a source id (may be an orphaned id no longer in the
  registry) and a target id.
- **Bulk (by prefix)** — enter a source prefix and a target prefix; every recorder
  id starting with the source prefix is remapped.

Either way you get a **preview** (how many states/statistics move, and how many get
discarded) before you confirm.

### The `history_mover.rename` action

Admin-only, and it returns response data. Great from *Developer Tools → Actions*
and from scripts.

Single pair, previewed first with **dry run**:

```yaml
action: history_mover.rename
data:
  old_entity_id: sensor.solax_pv_power
  new_entity_id: sensor.my_inverter_pv_power
  dry_run: true          # report only; change nothing
```

Bulk, applied:

```yaml
action: history_mover.rename
data:
  renames:
    - old_entity_id: sensor.solax_pv_power
      new_entity_id: sensor.my_inverter_pv_power
    - old_entity_id: sensor.solax_today_yield
      new_entity_id: sensor.my_inverter_today_yield
  on_conflict: replace   # replace (default) | skip | fail
```

| Field | Default | Meaning |
| --- | --- | --- |
| `old_entity_id` / `new_entity_id` | — | A single source → target pair. |
| `renames` | — | A list of `{old_entity_id, new_entity_id}` pairs (bulk). |
| `on_conflict` | `replace` | When the target already holds history of the same kind: `replace` it, `skip` the pair, or mark it `fail`. |
| `dry_run` | `false` | Report what *would* happen; change nothing. Call with *Return response* to see it. |
| `scan_references` | `true` | After moving, report config files that still mention the old id (see below). |

Provide **either** `old_entity_id` + `new_entity_id`, **or** `renames`. Within one
call, no id may appear twice — and not as both a source and a target (no swaps or
chains): the outcome would depend on processing order. Run those as separate calls,
in the order you intend.

### Example: migrating a device (e.g. SolaX → a different integration)

1. Set up the new integration; note the new entity ids.
2. Dry-run the rename (a `renames` list, or a bulk prefix swap) and read the preview.
3. Apply it.
4. Remove the old integration.
5. Update anything the **reference scan** flagged (see below), then reload/restart.

## How it works

The `states` and `statistics` tables don't store entity ids — they reference a
numeric `metadata_id` in `states_meta` / `statistics_meta`. Moving history is
therefore just:

1. delete the target's own rows for that stream (the discarded history), then
2. re-label the source's `states_meta` / `statistics_meta` row to the target id.

It all runs inside the recorder's own SQLAlchemy session, on the recorder thread,
as a single task — the same machinery Home Assistant uses for its built-in rename.
Afterwards the in-memory caches (`entity_id → metadata_id`, the `old_state_id`
link tracking, and the statistics meta cache) are invalidated so the live target
resolves to the adopted history on its next recorded state.

Because it goes through the recorder session, it works the same on **SQLite,
MariaDB/MySQL and PostgreSQL**.

## Reference scan (report only)

Moving history does not touch the places that *use* an id. With `scan_references`
on (the default), History Mover reports — and never edits — config files that still
mention the old id, as a persistent notification and in the action response. It
scans: root `configuration.yaml`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`,
`groups.yaml`, `templates.yaml`, `ui-lovelace.yaml`; `.storage` Lovelace dashboards;
and `.storage/core.config_entries` (UI-created helpers). Matches are whole-id only.

## Safety

- Admin-only action.
- **Dry run** first; the UI always previews before applying.
- Every applied move is logged (with row counts); previews, skips and noops log
  at debug — enable with `logger: {logs: {custom_components.history_mover: debug}}`.
- Take a backup before large migrations — the discard is not reversible.

## Limitations

- "Replace" **discards** the target's colliding history by design; it is not a merge
  of two timelines.
- The bulk UI does a prefix swap; for arbitrary mappings use the action with a
  `renames` list.
- The reference scan is a text search over the files listed above — treat it as a
  helpful heads-up, not a guarantee that every reference was found.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest tests/ --cov=custom_components.history_mover   # gate: ≥95%
.venv/bin/ruff check custom_components tests
.venv/bin/mypy                                                            # strict
```

All three run in CI, alongside `hassfest` and the HACS validator. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

The brand mark uses the *history* glyph from [Material Design Icons](https://pictogrammers.com/library/mdi/) (Apache-2.0).

## License

[MIT](LICENSE) © David Matscheko
