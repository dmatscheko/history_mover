"""Report-only scan for lingering references to a renamed entity id.

Moving an entity's *history* does not touch the places that *use* the id —
automations, scripts, scenes, dashboards, or UI-created helpers. This module
finds where a source id still appears so the user can update those by hand. It
never edits anything.

The scan is deliberately a text search over a curated set of configuration
files rather than a semantic walk of every integration: that keeps it robust
across Home Assistant versions and honest about what it looked at. Coverage is
listed in the README. Matches are whole-id only, so ``sensor.power`` does not
match ``sensor.power_total`` or ``binary_sensor.power``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from homeassistant.core import HomeAssistant

# Config-root YAML files that commonly hold entity ids. UI-managed automations,
# scripts and scenes are persisted to these same files, so this covers both
# YAML- and UI-authored config.
_ROOT_YAML_FILES = (
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "groups.yaml",
    "templates.yaml",
    "ui-lovelace.yaml",
)


@dataclass(slots=True)
class ReferenceHit:
    """One file/store in which a source id still appears, and how many times."""

    source: str
    count: int

    def as_dict(self) -> dict[str, object]:
        return {"source": self.source, "count": self.count}


async def async_scan_references(
    hass: HomeAssistant, entity_ids: Iterable[str]
) -> dict[str, list[ReferenceHit]]:
    """Return, per entity id, the config files/stores that still mention it.

    Reads files, so it runs in the executor. Ids with no lingering references
    are omitted from the result.
    """
    ids = {eid for eid in entity_ids if eid}
    if not ids:
        return {}
    config_dir = Path(hass.config.config_dir)
    return await hass.async_add_executor_job(_scan, config_dir, ids)


def _candidate_files(config_dir: Path) -> Iterator[tuple[Path, str]]:
    """Yield (path, human label) pairs to search."""
    for name in _ROOT_YAML_FILES:
        yield config_dir / name, name
    storage = config_dir / ".storage"
    # Lovelace dashboards (default + per-dashboard stores).
    for path in sorted(storage.glob("lovelace*")):
        yield path, f".storage/{path.name}"
    # Config entries carry the options of UI-created helpers (group, template,
    # utility_meter, threshold, derivative, ...), which reference entity ids.
    yield storage / "core.config_entries", ".storage/core.config_entries"


def _scan(config_dir: Path, entity_ids: set[str]) -> dict[str, list[ReferenceHit]]:
    patterns = {
        eid: re.compile(rf"(?<![\w.]){re.escape(eid)}(?![\w.])") for eid in entity_ids
    }
    results: dict[str, list[ReferenceHit]] = {eid: [] for eid in entity_ids}
    for path, label in _candidate_files(config_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # missing or unreadable — nothing to report from it
        for eid in entity_ids:
            count = len(patterns[eid].findall(text))
            if count:
                results[eid].append(ReferenceHit(label, count))
    return {eid: hits for eid, hits in results.items() if hits}
