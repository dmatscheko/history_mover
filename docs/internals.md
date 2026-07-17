# How History Mover works

For the curious, and for anyone deciding whether to trust this integration
with their database. Development setup, tests and quality gates live in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Moving history

The `states` and `statistics` tables don't store entity ids — they reference a
numeric `metadata_id` in `states_meta` / `statistics_meta`. Moving history is
therefore just:

1. delete the target's own rows for that stream (the discarded history), then
2. re-label the source's `states_meta` / `statistics_meta` row to the target id.

No per-state rewrite, whatever the row count. This is the same mechanism Home
Assistant's built-in rename uses — History Mover adds the "replace an occupied
target" half that the built-in refuses.

## Deleting history

The targeted delete and the orphan purge walk the same two meta tables the
other way around: every selected id — named explicitly, matched by domain, or
found orphaned — gets its rows and meta rows deleted. An id counts as
**orphaned** when it has no current state in the state machine *and* no
entity-registry entry; registry entries survive disabled entities, unloaded
integrations and restarts, so only genuinely removed entities fall through.
External statistics (`domain:object_id`) never have an entity and are never
candidates.

Deleting states also cleans up the deduplicated `state_attributes` rows that
no surviving state shares — the same bookkeeping Home Assistant core's purge
does. The optional repack runs core's own `repack_database`, exactly what
`recorder.purge` with `repack: true` runs.

## Why it is safe

- **Everything runs on the recorder thread**, as a single queued
  `RecorderTask`, inside one transaction of the recorder's own SQLAlchemy
  session. The recorder flushes pending writes before the task runs
  (`commit_before`), so counts are exact and no in-flight state is lost.
- **The orphan liveness snapshot cannot race a new entity.** It is taken from
  the recorder thread (via a thread-safe callback into the event loop) *after*
  that flush. A state always reaches the state machine before the recorder
  writes it — so every id visible in the database was either in the snapshot,
  or was already removed again, which is exactly an orphan.
- **Applying the orphan purge is refused while Home Assistant is starting**:
  entities that simply have not loaded yet (e.g. YAML platforms without a
  registry entry) would look orphaned.
- **The guided UI applies only what it previewed.** The apply is restricted to
  the previewed ids, and the orphan purge re-checks each id is still orphaned
  at apply time — nothing unseen is deleted, nothing revived is lost.
- **Caches are fixed after the commit.** The recorder keeps in-memory caches
  (`entity_id → metadata_id`, the `old_state_id` link tracking, the statistics
  meta cache, the shared-attributes cache). After a change they are evicted on
  the recorder thread, so a live target resolves to its adopted history — and
  a deleted id that records again starts cleanly — with no restart.
- **Log lines promise durable changes.** Applied operations are logged with
  row counts (`Moved history`, `Deleted history`, `Purged orphaned history`)
  only after the transaction committed; previews and no-ops log at debug —
  enable with `logger: {logs: {custom_components.history_mover: debug}}`.

## Database backends

History Mover works on everything the recorder supports — SQLite,
MariaDB/MySQL and PostgreSQL — by construction:

- Every statement is SQLAlchemy ORM through the recorder's own session and
  engine; there is no raw SQL. The constructs used (SELECT, COUNT, DISTINCT,
  IN, LIKE with ESCAPE, bulk UPDATE/DELETE) are the lowest common denominator
  of SQL.
- The one dialect-specific operation — the repack — is core's own
  `repack_database`, which branches per backend (`VACUUM` on SQLite and
  PostgreSQL, `OPTIMIZE TABLE` on MySQL/MariaDB).
- Deletes respect every foreign key on backends that enforce them: the
  self-referential `old_state_id` chain is nulled first, data rows go before
  their meta rows, and shared attribute rows are only dropped after proving no
  state references them.
- Large IN-lists are chunked by the recorder's per-engine bind-parameter
  limit, like core's purge.
- Delete/purge matching happens in Python over fetched meta rows, so backend
  collation rules cannot change which ids match.

One profile difference to core's purge: History Mover deletes a whole stream
per statement inside one transaction, while core purges in small yielding
batches. Correct on every backend, but on histories with millions of rows it
holds the recorder transaction longer — which is also why big operations can
take minutes.

## Code layout

| Module | Role |
| --- | --- |
| `mover.py` | The rename engine (validate, count, relabel, discard). |
| `purger.py` | The delete, orphan-purge and repack engines. |
| `db.py` | Shared row-level operations (count, discard, attribute cleanup). |
| `services.py` | The four admin actions; shapes engine reports into responses. |
| `config_flow.py` | The guided dialog; same engines, plus previews and the full-preview file. |
| `references.py` | The report-only reference scan. |
