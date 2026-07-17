# Actions — non-UI usage

Everything the guided dialog does is also available as **admin actions**, for
*Developer Tools → Actions*, scripts and automations. The three destructive
actions support a **dry run** and return **response data** — call them with
*Return response* to get the full report (unlike the dialog, the response is
never truncated).

- [`history_mover.rename`](#history_moverrename) — move history between ids
- [`history_mover.delete`](#history_moverdelete) — delete history by id/domain
- [`history_mover.purge_orphans`](#history_moverpurge_orphans) — delete orphaned history
- [`history_mover.repack`](#history_moverrepack) — reclaim freed disk space

## `history_mover.rename`

Moves recorder history — states and long-term statistics — from a source id
onto a target id. If the target already holds history of the same kind, it is
discarded and replaced, so a replacement sensor can adopt the old history and
keep recording into it.

Single pair, previewed first:

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
| `old_entity_id` / `new_entity_id` | — | A single source → target pair. The source may be an id that no longer exists. |
| `renames` | — | A list of `{old_entity_id, new_entity_id}` pairs (bulk). |
| `on_conflict` | `replace` | When the target already holds history of the same kind: `replace` it, `skip` the pair, or mark it `fail`. |
| `dry_run` | `false` | Report what *would* happen; change nothing. |
| `scan_references` | `true` | After moving, report config files that still mention the old id (see below). |

Provide **either** `old_entity_id` + `new_entity_id`, **or** `renames`. Within
one call, no id may appear twice — and not as both a source and a target (no
swaps or chains): the outcome would depend on processing order. Run those as
separate calls, in the order you intend.

The response carries one entry per pair under `renames`, each with a `status`
(`renamed`, `replaced`, `skipped`, `failed` or `noop`), the moved and
discarded row counts, and a `detail` sentence; plus `references` when the
reference scan ran.

## `history_mover.delete`

Deletes the **complete** history — states and long-term statistics — of
exactly what you name: entity ids, whole domains, or both. Ids are matched
exactly as stored in the recorder, so ids that no longer exist in Home
Assistant (even malformed leftovers) can be addressed. Live entities keep
their current state and start a fresh history on their next recorded state.

```yaml
action: history_mover.delete
data:
  entity_ids:
    - sensor.old_pv_power
    - sensor.retired_meter
  domains:
    - camera
  dry_run: true          # preview first; then apply without it
```

| Field | Default | Meaning |
| --- | --- | --- |
| `entity_ids` | — | Exact ids whose history to delete — including ids that no longer exist. |
| `domains` | — | Whole domains, e.g. `camera` — the history of every entity of that domain. |
| `dry_run` | `false` | Report what would be deleted (with row counts); change nothing. |
| `repack` | `false` | Repack the database afterwards (see `history_mover.repack`). |

Provide at least one of `entity_ids` / `domains`. The response lists each
deleted id with row counts under `deletions`, plus `not_found_entity_ids` and
`not_found_domains` for selection parts that matched nothing — so a typo shows
up in the dry run instead of silently deleting nothing. External statistics
(ids with a colon) are never touched.

## `history_mover.purge_orphans`

Deletes every recorder history that **no existing entity writes into
anymore**: the id has no current state in the state machine and no entry in
the entity registry. That is what removed integrations, deleted helpers and
old renames leave behind — including the orphaned long-term statistics that
`recorder.purge_entities` cannot remove.

```yaml
action: history_mover.purge_orphans
data:
  dry_run: true          # report only; change nothing
```

Then apply, optionally reclaiming the freed disk space:

```yaml
action: history_mover.purge_orphans
data:
  repack: true
```

| Field | Default | Meaning |
| --- | --- | --- |
| `dry_run` | `false` | Report which histories would be deleted (with row counts); change nothing. |
| `repack` | `false` | Repack the database afterwards (see `history_mover.repack`). |

The response lists each orphan with its row counts under `orphans`. What is
**never** touched:

- anything with a current state — including entities the recorder is filtered
  to not record;
- anything still in the entity registry — covering **disabled entities** and
  integrations that are temporarily unloaded or failing;
- **external statistics** (ids with a colon, e.g. imported energy data): they
  have no entity by design.

Applying is refused while Home Assistant is still starting, because entities
that simply have not loaded yet would look orphaned. For the same reason, run
it on a fully started system — an entity provided by a YAML platform without a
unique id (no registry entry) counts as alive only through its current state.

## `history_mover.repack`

Repacks the database **without deleting anything** — the same
`VACUUM` / `OPTIMIZE TABLE` that `recorder.purge` runs with `repack: true`,
just without having to purge anything first. Useful after deletions (by this
integration, `recorder.purge*`, or anything else) that ran without a repack.
It takes no fields:

```yaml
action: history_mover.repack
```

Rewriting the file can take a while on a large database and temporarily needs
additional free disk space of roughly the database size.

## Example: migrating a device (e.g. SolaX → a different integration)

1. Set up the new integration; note the new entity ids.
2. Dry-run the rename (a `renames` list, or the bulk UI) and read the preview.
3. Apply it.
4. Remove the old integration.
5. Update anything the **reference scan** flagged, then reload/restart.
6. Optionally run `history_mover.purge_orphans` to sweep up whatever the old
   integration left behind, with `repack: true` to get the disk space back.

## The reference scan (report only)

Moving history does not touch the places that *use* an id. With
`scan_references` on (the default), History Mover reports — and never edits —
config files that still mention the old id, as a persistent notification and
in the action response. It scans: root `configuration.yaml`,
`automations.yaml`, `scripts.yaml`, `scenes.yaml`, `groups.yaml`,
`templates.yaml`, `ui-lovelace.yaml`; `.storage` Lovelace dashboards; and
`.storage/core.config_entries` (UI-created helpers). Matches are whole-id
only. Treat it as a helpful heads-up, not a guarantee that every reference was
found.
