"""Find and purge orphaned recorder history (states + statistics).

The recorder never deletes a history on its own when its entity disappears —
after removing an integration, renaming outside the UI, or deleting entities,
``states_meta`` / ``statistics_meta`` streams linger and keep their rows
through every nightly purge. Home Assistant's own tools cover this only half
way: ``recorder.purge_entities`` needs the ids typed in by hand and only
purges states, and the Developer Tools statistics tab fixes one statistic at
a time.

This module finds every such orphan in one sweep and deletes it — states,
long-term and short-term statistics, the meta rows, and the shared attribute
rows nothing references anymore — optionally repacking the database afterwards
(the same ``repack_database`` that ``recorder.purge`` with ``repack: true``
runs).

What counts as orphaned: an id with recorder history that has **no current
state** in the state machine and **no entity-registry entry**. Registry
entries survive disabled entities, unloaded integrations and restarts, so
those all stay protected; only ids nothing can write into anymore fall
through. External statistics (``domain:object_id``, e.g. imported energy
data) never have an entity and are never touched.

Timing safety, in the same spirit as the mover:

* Everything runs as one ``RecorderTask`` on the recorder thread, inside one
  transaction. ``commit_before`` flushes pending writes first.
* The liveness snapshot is taken *from the recorder thread* (via a threadsafe
  callback into the event loop) after that flush. A state always reaches the
  state machine before the recorder writes it, so every id visible in the
  database was either in the snapshot — or was already removed again, which is
  exactly an orphan. A brand-new entity can never be mistaken for one.
* Applying is refused while Home Assistant is still starting: entities that
  simply have not loaded yet (e.g. YAML platforms without a registry entry)
  would look orphaned.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import States, StatesMeta, StatisticsMeta
from homeassistant.components.recorder.repack import repack_database
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.async_ import run_callback_threadsafe

from .const import RECORDER_TASK_TIMEOUT
from .db import (
    count_rows,
    count_statistics,
    delete_unused_attributes,
    discard_states,
    discard_statistics,
)

if TYPE_CHECKING:
    from homeassistant.components.recorder import Recorder

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PurgeOutcome:
    """One orphaned history and what was (or, for a dry run, would be) deleted."""

    entity_id: str
    applied: bool
    deleted_states: int = 0
    deleted_statistics: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for the service response (JSON-serialisable)."""
        return asdict(self)


@callback
def _alive_entity_ids(hass: HomeAssistant) -> set[str]:
    """Ids something can still write into — these are never orphans.

    Alive means: a current state in the state machine (covers unregistered
    entities such as YAML platforms without a unique id) or an entity-registry
    entry (covers disabled entities and integrations that are temporarily not
    loaded). Registry entries for *deleted* entities do not count — a removed
    integration is precisely what leaves the orphans this module cleans up.
    """
    return set(hass.states.async_entity_ids()) | set(er.async_get(hass).entities)


async def async_purge_orphans(
    hass: HomeAssistant,
    *,
    dry_run: bool = False,
    repack: bool = False,
    restrict_to: set[str] | None = None,
) -> list[PurgeOutcome]:
    """Delete every orphaned history; return one outcome per orphan, sorted.

    A dry run only reports what would be deleted. ``restrict_to`` limits the
    purge to ids in that set — the guided flow passes its previewed ids, so it
    never deletes anything the user has not seen (the orphan check itself is
    re-run either way, so an id that came back to life is always spared).
    With ``repack``, the database is repacked after a successful purge.
    """
    if not dry_run and hass.state is not CoreState.running:
        raise ServiceValidationError(
            "Purging orphaned history is only allowed once Home Assistant has"
            " fully started — before that, entities that are just not loaded"
            " yet would be mistaken for orphans."
        )
    instance = get_instance(hass)
    task = _PurgeOrphansTask(dry_run=dry_run, repack=repack, restrict_to=restrict_to)
    instance.queue_task(task)
    finished = await hass.async_add_executor_job(task.done.wait, RECORDER_TASK_TIMEOUT)
    if not finished:
        raise HomeAssistantError(
            "Timed out waiting for the recorder to finish purging orphaned"
            " history. The queued purge (and repack, if requested) may still"
            " complete in the background — check the log for 'Purged orphaned"
            " history' entries before retrying."
        )
    if task.error is not None:
        raise HomeAssistantError(
            f"History Mover failed while purging orphaned history: {task.error}"
        ) from task.error
    return task.outcomes


@dataclass(slots=True)
class _PurgeOrphansTask(RecorderTask):
    """Runs the whole purge on the recorder thread and signals completion.

    ``commit_before`` (inherited, True) makes the recorder flush pending
    states before ``run`` — counts are exact, and the liveness snapshot taken
    inside ``run`` is strictly newer than any row this task can see.
    """

    dry_run: bool
    repack: bool
    restrict_to: set[str] | None
    outcomes: list[PurgeOutcome] = field(default_factory=list)
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def run(self, instance: Recorder) -> None:
        try:
            self.outcomes = _run_purge(instance, self.dry_run, self.restrict_to)
            if self.repack and not self.dry_run:
                # After the purge committed, reclaim the freed space — the
                # same repack recorder.purge's own repack option runs. Done
                # even for zero orphans: the user asked for a repack.
                _LOGGER.info("Repacking the database to reclaim freed space")
                repack_database(instance)
        except Exception as err:  # surfaced to the caller via .error
            _LOGGER.exception("History Mover purge task failed")
            self.error = err
        finally:
            self.done.set()


def _run_purge(
    instance: Recorder, dry_run: bool, restrict_to: set[str] | None
) -> list[PurgeOutcome]:
    """Find orphans and (unless dry_run) delete them in one transaction."""
    # Taken from the recorder thread, so it post-dates the commit_before flush;
    # see the module docstring for why this closes the new-entity race.
    alive = run_callback_threadsafe(
        instance.hass.loop, _alive_entity_ids, instance.hass
    ).result()

    outcomes: list[PurgeOutcome] = []
    purged_ids: set[str] = set()
    removed_attributes: set[int] = set()
    with session_scope(session=instance.get_session()) as session:
        states_by_id = {
            entity_id: metadata_id
            for metadata_id, entity_id in session.query(
                StatesMeta.metadata_id, StatesMeta.entity_id
            )
            if entity_id and entity_id not in alive
        }
        stats_by_id = {
            statistic_id: meta_id
            for meta_id, statistic_id in session.query(
                StatisticsMeta.id, StatisticsMeta.statistic_id
            ).filter(StatisticsMeta.source == "recorder")
            # Only entity-shaped ids (domain.object) can be orphans; external
            # statistics (domain:object) have no entity by design.
            if statistic_id
            and "." in statistic_id
            and ":" not in statistic_id
            and statistic_id not in alive
        }
        orphan_ids = set(states_by_id) | set(stats_by_id)
        if restrict_to is not None:
            orphan_ids &= restrict_to

        attributes_ids: set[int] = set()
        for entity_id in sorted(orphan_ids):
            states_meta_id = states_by_id.get(entity_id)
            stats_meta_id = stats_by_id.get(entity_id)
            outcome = PurgeOutcome(
                entity_id,
                applied=not dry_run,
                deleted_states=(
                    count_rows(session, States, states_meta_id)
                    if states_meta_id is not None
                    else 0
                ),
                deleted_statistics=(
                    count_statistics(session, stats_meta_id)
                    if stats_meta_id is not None
                    else 0
                ),
            )
            outcomes.append(outcome)
            if dry_run:
                continue
            if states_meta_id is not None:
                attributes_ids |= discard_states(session, states_meta_id)
            if stats_meta_id is not None:
                discard_statistics(session, stats_meta_id)
            purged_ids.add(entity_id)
        if attributes_ids:
            # One pass over everything the deleted states referenced; only
            # rows no surviving state shares are dropped (see db.py).
            removed_attributes = delete_unused_attributes(
                instance, session, attributes_ids
            )

    # Log only after the transaction committed — these lines promise durable
    # changes (a failed run is logged by the task's error handler instead).
    for outcome in outcomes:
        if outcome.applied:
            _LOGGER.info(
                "Purged orphaned history %s: deleted %d states / %d statistics rows",
                outcome.entity_id,
                outcome.deleted_states,
                outcome.deleted_statistics,
            )
        else:
            _LOGGER.debug(
                "Would purge orphaned history %s: %d states / %d statistics rows",
                outcome.entity_id,
                outcome.deleted_states,
                outcome.deleted_statistics,
            )

    if purged_ids:
        # Post-commit, recorder-thread cache fixes, exactly like the mover: an
        # orphan that comes back to life must resolve fresh metadata, not a
        # cached id pointing at deleted rows.
        instance.states_meta_manager.evict_purged(purged_ids)
        instance.states_manager.evict_purged_entity_ids(purged_ids)
        instance.statistics_meta_manager.reset()
    if removed_attributes:
        instance.state_attributes_manager.evict_purged(removed_attributes)
        _LOGGER.debug(
            "Deleted %d unused shared attribute rows", len(removed_attributes)
        )
    return outcomes
