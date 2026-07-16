# Code review — 2026-07-16

A full read of the integration (`custom_components/history_mover/`, `tests/`, CI and
metadata files), verified against Home Assistant 2026.7 sources and an in-memory
recorder. Baseline before any change: 33 tests green, 100 % coverage, ruff and mypy
clean. Every finding below was **confirmed** — behavioral bugs by a reproduction
test against a real recorder, the rest against the installed HA source.

Statuses are updated as fixes land; each fix is its own commit.

## Bugs

### B1 (high) — an id may be both a source and a target in one call; order decides what survives

- **Where:** [services.py](custom_components/history_mover/services.py) `_reject_ambiguous`,
  [mover.py](custom_components/history_mover/mover.py) `_run_batch`.
- **What:** validation rejects duplicate sources and duplicate targets but not an id
  appearing on *both* sides. Pairs are processed strictly in order, so the result of
  such a batch depends on listing order — and with the default `on_conflict: replace`
  it silently destroys history.
- **Repro (confirmed):** `renames: [a→b, b→a]` (a swap): b's 3 states are discarded,
  a's history is relabelled to b and then moved *back* to a. Net effect: b's history
  destroyed, nothing swapped, response reports success. `[a→b, b→c]` similarly moves
  a's history to c *through* b and discards b's.
- **Also reachable from the UI:** the bulk prefix flow generates exactly this shape
  whenever the target prefix extends the source prefix (e.g. `sensor.a_` → `sensor.a_x_`),
  and the flow performs no ambiguity validation at all (see C1).
- **Fix:** batch validation belongs to the shared engine (`async_move_history`), not the
  service layer: same-id, duplicate source, duplicate target, and a new
  source∩target overlap check. Split calls stay possible and are order-explicit.
- **Status:** fixed — validation moved into `async_move_history` with a new
  source∩target overlap check; regression tests for swap and chain batches.

### B2 (high) — the options flow accepts invalid/uppercase ids and strands history

- **Where:** [config_flow.py](custom_components/history_mover/config_flow.py)
  `async_step_single` (and `async_step_bulk`, see B3).
- **What:** the service schema validates ids with `cv.entity_id` (which also
  lowercases), the flow only `.strip()`s. A target like `Sensor.New` is accepted,
  previewed, and applied.
- **Repro (confirmed):** single flow with target `Sensor.New` relabels the history to
  that id. Live recording only ever writes lowercase ids, so the history is stranded
  under an id nothing will ever record into — and because `cv.entity_id` lowercases,
  the service cannot address the stranded id to move it back. Only manual SQL can.
- **Fix:** the flow validates and normalises both ids with the same `cv.entity_id`
  validator the service uses (shows an `invalid_entity_id` form error); the engine
  additionally refuses structurally invalid *target* ids as a backstop for any caller.
- **Status:** fixed — the flow validates both ids with `cv.entity_id` (per-field
  `invalid_entity_id` errors); the engine refuses invalid target ids for any caller.

### B3 (medium) — bulk flow: an empty source prefix matches every recorder id

- **Where:** [config_flow.py](custom_components/history_mover/config_flow.py)
  `async_step_bulk`, [mover.py](custom_components/history_mover/mover.py)
  `_list_history_ids` (`LIKE '%'`).
- **What:** an empty (or whitespace) source prefix produces a pattern that matches the
  whole database; with a domain-less target prefix like `x` this renames *every*
  history id (`light.evb` → `xlight.evb` — a structurally valid id, so target-format
  validation alone would not block it). Prefixes are also not lowercased, unlike ids
  everywhere else.
- **Repro (confirmed):** bulk flow with `old_prefix=""`, `new_prefix="x"` reached the
  confirm step listing every entity in the recorder.
- **Fix:** lowercase both prefixes, require a non-empty source prefix, validate every
  generated target id, and show a dedicated error when generated targets overlap the
  source set (the friendly-UI face of B1's engine check).
- **Status:** fixed — prefixes are stripped/lower-cased; empty source prefix,
  invalid generated targets, and source∩target overlap each get a form error.

### B4 (low) — history id listing runs on the wrong executor

- **Where:** [mover.py](custom_components/history_mover/mover.py) `async_list_history_ids`.
- **What:** the docstring says "Runs in the recorder's executor", but the code uses
  `hass.async_add_executor_job`. On a foreign thread the recorder's `RecorderPool`
  falls back to NullPool behaviour and opens a fresh database connection per call.
  HA core routes recorder reads through `get_instance(hass).async_add_executor_job`.
- **Status:** fixed — listing runs via `get_instance(hass).async_add_executor_job`.

### B5 (low) — README promises "Every action is logged"; the engine logs nothing on success

- **Where:** [mover.py](custom_components/history_mover/mover.py) (`_LOGGER` used only
  for unexpected exceptions), [README.md](README.md) "Safety".
- **What:** a destructive admin tool should leave a trace. Also, the timeout error does
  not tell the user that the queued task may still complete afterwards.
- **Fix:** log each applied pair (info) and each preview/skip/noop (debug); extend the
  timeout message.
- **Status:** open — planned fix: `log every outcome and clarify the timeout error`

### B6 (low) — a failure while applying from the UI aborts the flow with "Unknown error"

- **Where:** [config_flow.py](custom_components/history_mover/config_flow.py)
  `async_step_confirm`.
- **What:** `async_perform_rename` raises `HomeAssistantError` on engine failure or
  timeout; the flow does not catch it, so the user gets the generic unknown-error
  toast and loses the flow state.
- **Fix:** catch `HomeAssistantError` on apply and re-show the confirm form with an
  `apply_failed` error.
- **Status:** open — planned fix: `show a form error when applying from the flow fails`

### B7 (low) — the confirm preview is unbounded

- **Where:** [config_flow.py](custom_components/history_mover/config_flow.py)
  `_format_preview`.
- **What:** the README advertises moving "hundreds of entities in one operation"; the
  confirm dialog then renders hundreds of markdown lines.
- **Fix:** a totals line for multi-pair batches and a cap on listed pairs
  ("… and N more").
- **Status:** open — planned fix: `cap the confirm preview and add batch totals`

## Equivalent concepts expressed inconsistently

### C1 — request validation exists in three divergent shapes

- **Where:** service ([services.py](custom_components/history_mover/services.py):
  `cv.entity_id` schema + `_reject_ambiguous`), options flow
  ([config_flow.py](custom_components/history_mover/config_flow.py): ad-hoc `strip()`
  + inline same-id check, nothing else), engine
  ([mover.py](custom_components/history_mover/mover.py): no validation).
- **Why it matters:** the gaps between the copies are exactly bugs B1–B3. The UI and
  the service are documented as "the same engine, identical behaviour"
  (CONTRIBUTING.md), but their input handling disagreed.
- **Alignment (best of each):** the engine — the one shared choke point — owns batch
  semantics validation (B1) and target-format backstop (B2); both callers reuse
  `cv.entity_id` for per-field validation/normalisation (the service already did; the
  flow now does, with friendly per-field errors, which the service cannot show).
- **Status:** aligned — engine owns batch validation (B1) and the target-format
  backstop (B2); both callers normalise per-field input via `cv.entity_id`
  semantics (B2/B3).

### C2 — flow field-name constants live apart from their siblings

- **Where:** `ATTR_OLD_PREFIX`/`ATTR_NEW_PREFIX` in
  [config_flow.py](custom_components/history_mover/config_flow.py); every other
  service/flow field name lives in [const.py](custom_components/history_mover/const.py).
- **Alignment:** move both to `const.py`.
- **Status:** open — planned fix: `move prefix field names to const`

### C3 — two shapes for "count rows for a metadata id"

- **Where:** [mover.py](custom_components/history_mover/mover.py): `_count(session,
  model, metadata_id)` is parameterised but only ever counts `States`;
  `_count_statistics(session, metadata_id)` hardcodes an internal loop over the two
  statistics models.
- **Alignment:** one `_count_rows(session, model, metadata_id)` primitive; the
  statistics variant sums it over `Statistics` + `StatisticsShortTerm`.
- **Status:** open — planned fix: `unify row counting on one helper`

### C4 — hand-written dataclass→dict serialisation

- **Where:** `RenameOutcome.as_dict`
  ([mover.py](custom_components/history_mover/mover.py)) enumerates all nine fields by
  hand (drift risk when a field is added); `ReferenceHit.as_dict`
  ([references.py](custom_components/history_mover/references.py)) does the same in
  miniature.
- **Alignment:** both use `dataclasses.asdict`.
- **Status:** open — planned fix: `use dataclasses.asdict for response serialisation`

### C5 — German wording for "bulk"

- **Where:** [translations/de.json](custom_components/history_mover/translations/de.json):
  standalone "Sammel" ("Sammel (nach Präfix)", "Verlauf verschieben — Sammel",
  "Umbenennungen (Sammel)") — "Sammel-" only exists as a bound prefix in German.
- **Alignment:** "Mehrere (nach Präfix)" / "— mehrere" / "Umbenennungen (mehrere)".
- **Status:** open — planned fix: `use standard german wording for the bulk option`

## Considered and deliberately kept

- **"rename" vs "move" vocabulary.** The service is `rename`, the engine is `mover` /
  `async_move_history`, statuses say `renamed`. This is layered on purpose: the
  user-facing *action* renames an id; the database-level *effect* moves history. The
  README titles the service "Rename (move history)" and each module docstring states
  its side. Renaming either layer would churn a public API for no clarity gain.
- **`_discard_states` nulls `old_state_id` only within the discarded metadata_id.**
  HA's purge nulls globally (any row referencing a deleted row) because it deletes
  arbitrary row sets. Here the deleted set is always *entire chains* of one
  metadata_id, the recorder thread is ours during the task, and the caches are evicted
  before the next insert — a cross-metadata inbound link is not constructible through
  this integration or live recording. Matching purge would require materialising ids
  in batches (MySQL cannot self-subquery in UPDATE). Revisit only if an FK failure is
  ever reported.
- **The generated preview summary is English-only** while the surrounding dialog is
  translated. Translating generated fragments needs placeholder plumbing that the
  options-flow description does not support well; the counts and ids are the content.
- **A fixed `notification_id` replaces the previous reference report** rather than
  stacking one notification per rename — deduplication is the intended behaviour.
- **The service stays registered after the entry unloads** — documented in
  `__init__.py`; the entry only exists to host the options flow.
- **README and CONTRIBUTING repeat the dev commands** — intentional redundancy for
  the two audiences.
- **`LIKE` prefix matching is case-insensitive on SQLite** — recorder ids are
  lowercase (and, after B2/B3, so is all input), so no observable difference.
