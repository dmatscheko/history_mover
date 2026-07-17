"""Constants for the History Mover integration."""

from typing import Final

DOMAIN: Final = "history_mover"

# The four admin services: `rename` moves recorder history from one entity id
# onto another (one call carries one or many pairs); `delete` removes the
# history of explicitly named entity ids and/or whole domains; `purge_orphans`
# deletes every history no existing entity writes into anymore; `repack`
# rewrites the database file to reclaim freed space without deleting anything.
SERVICE_RENAME: Final = "rename"
SERVICE_DELETE: Final = "delete"
SERVICE_PURGE_ORPHANS: Final = "purge_orphans"
SERVICE_REPACK: Final = "repack"

# Service / flow field names.
ATTR_OLD_ENTITY_ID: Final = "old_entity_id"
ATTR_NEW_ENTITY_ID: Final = "new_entity_id"
ATTR_RENAMES: Final = "renames"
ATTR_ON_CONFLICT: Final = "on_conflict"
ATTR_DRY_RUN: Final = "dry_run"
ATTR_SCAN_REFERENCES: Final = "scan_references"
ATTR_REPACK: Final = "repack"
ATTR_ENTITY_IDS: Final = "entity_ids"
ATTR_DOMAINS: Final = "domains"
# Flow-only field names (the bulk prefix step).
ATTR_OLD_PREFIX: Final = "old_prefix"
ATTR_NEW_PREFIX: Final = "new_prefix"

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
