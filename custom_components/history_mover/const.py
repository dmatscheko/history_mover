"""Constants for the History Mover integration."""

from typing import Final

DOMAIN: Final = "history_mover"

# The one admin service. It moves recorder history from one entity id onto
# another; a single call carries one or many rename pairs.
SERVICE_RENAME: Final = "rename"

# Service / flow field names.
ATTR_OLD_ENTITY_ID: Final = "old_entity_id"
ATTR_NEW_ENTITY_ID: Final = "new_entity_id"
ATTR_RENAMES: Final = "renames"
ATTR_ON_CONFLICT: Final = "on_conflict"
ATTR_DRY_RUN: Final = "dry_run"
ATTR_SCAN_REFERENCES: Final = "scan_references"

# What to do when the target id already holds history of the same kind (states
# or statistics) that the source is about to move onto it.
CONFLICT_REPLACE: Final = "replace"  # discard the target's colliding history, then adopt
CONFLICT_SKIP: Final = "skip"  # leave this pair untouched
CONFLICT_FAIL: Final = "fail"  # abort this pair and report it as failed
CONFLICT_MODES: Final = [CONFLICT_REPLACE, CONFLICT_SKIP, CONFLICT_FAIL]
DEFAULT_ON_CONFLICT: Final = CONFLICT_REPLACE

# Per-pair outcome status strings (also used as translation keys in the UI).
STATUS_RENAMED: Final = "renamed"  # target was free — a plain rename, history followed
STATUS_REPLACED: Final = "replaced"  # target had colliding history — discarded, then adopted
STATUS_SKIPPED: Final = "skipped"  # collision + on_conflict=skip
STATUS_FAILED: Final = "failed"  # collision + on_conflict=fail, or an error
STATUS_NOOP: Final = "noop"  # the source had no recorder history to move

# How long the caller waits for the recorder thread to finish the queued rename
# task before giving up (seconds). A large migration deletes and re-labels many
# rows; this is generous on purpose.
RECORDER_TASK_TIMEOUT: Final = 600
