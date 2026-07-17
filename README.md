# History Mover

[![HACS: custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Tests](https://github.com/dmatscheko/history_mover/actions/workflows/test.yml/badge.svg)](https://github.com/dmatscheko/history_mover/actions/workflows/test.yml)
[![Validate](https://github.com/dmatscheko/history_mover/actions/workflows/validate.yml/badge.svg)](https://github.com/dmatscheko/history_mover/actions/workflows/validate.yml)

---
> ONLY TESTED ON MY PERSONAL HA INSTALLATION — **USE AT YOUR OWN RISK**
---

A Home Assistant integration for tidying up the recorder database: **move**
history (states *and* long-term statistics) from one entity onto another, or
from every id with one prefix onto another prefix at once; **delete** the
history of chosen entities or whole domains; **purge** orphaned histories that
nothing writes into anymore; and **repack** the database to reclaim the freed
disk space — always with a preview before anything changes.

## Getting started

1. **Install the integration.**
   - *HACS (recommended):* HACS → ⋮ → **Custom repositories** → add
     `https://github.com/dmatscheko/history_mover` (category *Integration*),
     then install **History Mover** and restart Home Assistant.
   - *Manual:* copy `custom_components/history_mover` into your
     `config/custom_components/` folder and restart.
2. **Add it to Home Assistant.** Go to **Settings → Devices & services**, click
   **Add integration**, and pick **History Mover**. It now appears as a card
   among your configured integrations.
3. **Open the tools.** On the History Mover card, click **Configure** (the
   gear). That guided dialog is where everything happens — it is the part most
   users will use.

> [!WARNING]
> These tools edit the recorder database directly, and nothing they do can be
> undone. You always get a **preview** first, and nothing changes until you
> confirm on the preview screen — still, take a backup before large operations.

## What you can do

The **Configure** dialog offers five tools:

- **Move single entity** — move the recorder history of one entity id onto
  another, so a replacement sensor adopts the long history of the one it
  supersedes and keeps recording into it. The source may be an id that no
  longer exists. If the target already holds history of the same kind, that
  history is discarded and replaced (deliberately — it is not a merge).
- **Bulk move (by prefix)** — the same, for every id starting with a prefix;
  handy when migrating a whole device to a different integration.
- **Delete history (by entity or domain)** — delete the complete history of
  exactly the entity ids you enter (existing or not) and/or of every entity of
  a domain. Live entities keep their current state and start a fresh history.
- **Purge orphaned history** — find and delete every history that no existing
  entity writes into anymore: the leftovers of removed integrations and
  deleted entities. Anything with a current state or an entity-registry entry
  (disabled entities included) is protected, external statistics are never
  touched, and it refuses to apply while Home Assistant is still starting.
- **Repack the database** — reclaim the disk space freed by earlier deletions
  by rewriting the database file (the same repack as `recorder.purge`), without
  deleting anything. Temporarily needs extra free disk space of roughly the
  database's size.

Every preview shows the affected ids with row counts and applies only what it
showed. The dialog lists the first 15 entries; the **complete list is written
to `history_mover_preview.md`** in your config folder (overwritten by each new
preview), so even a cleanup of thousands of entries can be reviewed in full.

> [!NOTE]
> Some of this takes time. Moving or deleting very large histories can run for
> a while, and repacking a big database can easily take **minutes**. Leave the
> dialog open while it works; if a step reports a timeout, the operation may
> still finish in the background — check the Home Assistant log before
> retrying.

After a move, History Mover also **reports** — and never edits — configuration
files that still mention the old id (automations, scripts, dashboards, …) as a
persistent notification.

## Good to know

- All tools are admin-only.
- Every applied change is logged with row counts (`Moved history`,
  `Deleted history`, `Purged orphaned history` lines).
- The bulk UI does a prefix swap; arbitrary mappings, scripting, dry runs and
  response data are available through the actions (see below).
- Works on every recorder database: SQLite, MariaDB/MySQL and PostgreSQL.

## Why this exists

Home Assistant already moves history along when you rename a **registered**
entity to a **free** id through the UI. History Mover fills the gaps around
that: adopting the history of an **occupied** target id (the built-in rename
refuses), rescuing history whose integration is long gone, and doing it for
hundreds of entities at once. On the cleanup side, the built-in
`recorder.purge_entities` wants every id typed in by hand and only purges
states — orphaned long-term statistics survive it entirely.

## Advanced usage and internals

- **[Actions — non-UI usage](docs/actions.md)**: `history_mover.rename`,
  `.delete`, `.purge_orphans` and `.repack` with all fields, YAML examples and
  response data, for Developer Tools, scripts and automations.
- **[How it works](docs/internals.md)**: what happens under the hood, why it is
  safe, and how the different database backends are handled.
- **[Contributing](CONTRIBUTING.md)**: development setup, tests and the quality
  gates.

## Credits

The brand mark uses the *history* glyph from
[Material Design Icons](https://pictogrammers.com/library/mdi/) (Apache-2.0).

## License

[MIT](LICENSE) © David Matscheko
