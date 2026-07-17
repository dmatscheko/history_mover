# Contributing

Thanks for helping! History Mover is a small, focused integration — the
recorder engines (rename in `mover.py`; targeted delete, orphan purge and
repack in `purger.py`), four admin services, a guided flow, and a report-only
reference scan. How the engines work under the hood is described in
[docs/internals.md](docs/internals.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest tests/ --cov=custom_components.history_mover  # gate: ≥95%
.venv/bin/ruff check custom_components tests
.venv/bin/mypy                                                           # strict
```

All three must pass; CI runs the same commands, plus `hassfest` and the HACS
validator. Points worth knowing:

- The engine ([`mover.py`](custom_components/history_mover/mover.py)) is the heart.
  It re-labels `states_meta` / `statistics_meta` and deletes the discarded rows in
  one transaction on the recorder thread, then evicts the recorder's in-memory
  caches so live recording continues. If you touch it, keep the end-to-end tests in
  [`tests/test_mover.py`](tests/test_mover.py) — especially "replace and continue
  recording" — green against a real in-memory recorder.
- The service and the options flow both call `async_perform_rename`, so they behave
  identically. Add behaviour there, not in one of them.
- `strings.json` is the translation source — mirror changes into
  `translations/en.json` (a copy) and `translations/de.json` (translate). Keep
  `services.yaml` field keys in sync with the `services` block of `strings.json`.

## Brand assets

The SVG sources live in [`support/`](support/); the shipped PNGs are in
`custom_components/history_mover/brand/`. To regenerate all of them, run
[`support/render.sh`](support/render.sh) (needs `rsvg-convert`).

For listing in the HACS default store, the same images should also be submitted to
[home-assistant/brands](https://github.com/home-assistant/brands).

## Commit messages

One line, imperative, lower-case — no body and no trailers.

- Start with a lower-case verb — `add`, `fix`, `move`, `rename`, `refactor`, `drop`, `bump`, `make`, …
- Say *what* changed, and *why* when it isn't obvious. One line, no trailing period; aim for < 80 chars.
- One logical change per commit.

```
move statistics as well as states when renaming
evict old_state_id tracking so the target keeps recording
add dry-run preview to the guided flow
```

## Reporting problems

Use the issue template. Include the exact entity ids, your recorder backend, and —
for anything subtle — debug logs
(`logger: {logs: {custom_components.history_mover: debug}}`).
