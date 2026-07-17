"""Delete recorder history: orphaned streams, chosen entities, whole domains.

The recorder never deletes a history on its own when its entity disappears —
after removing an integration, renaming outside the UI, or deleting entities,
``states_meta`` / ``statistics_meta`` streams linger and keep their rows
through every nightly purge. Home Assistant's own tools cover this only half
way: ``recorder.purge_entities`` needs the ids typed in by hand and only
purges states, and the Developer Tools statistics tab fixes one statistic at
a time.

Two engines share the machinery in this module:

* **Purge orphans** finds every stream whose id has **no current state** in
  the state machine and **no entity-registry entry**, and deletes it in one
  sweep. Registry entries survive disabled entities, unloaded integrations
  and restarts, so those all stay protected; only ids nothing can write into
  anymore fall through.
* **Targeted delete** removes the history of explicitly named entity ids
  (matched exactly as stored, so ids that no longer exist — even malformed
  ones — can be addressed) and/or of whole domains.
* **Repack** on its own rewrites the database file without deleting anything —
  for reclaiming the space of deletions that ran without it.

The two delete flavours remove states, long-term and short-term statistics,
the meta rows, and the shared attribute rows nothing references anymore —
optionally repacking the database afterwards (the same ``repack_database``
that ``recorder.purge`` with ``repack: true`` runs). External statistics
(``domain:object_id``, e.g. imported energy data) never have an entity and
are never touched.

Timing safety, in the same spirit as the mover:

* Everything runs as one ``RecorderTask`` on the recorder thread, inside one
  transaction. ``commit_before`` flushes pending writes first.
* The orphan liveness snapshot is taken *from the recorder thread* (via a
  threadsafe callback into the event loop) after that flush. A state always
  reaches the state machine before the recorder writes it, so every id
  visible in the database was either in the snapshot — or was already removed
  again, which is exactly an orphan. A brand-new entity can never be mistaken
  for one.
* Applying the orphan purge is refused while Home Assistant is still
  starting: entities that simply have not loaded yet (e.g. YAML platforms
  without a registry entry) would look orphaned. The targeted delete has no
  such guard — its selection is explicit, not inferred.
"""

from __future__ import annotations

import logging
import re
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
    from collections.abc import Iterable

    from homeassistant.components.recorder import Recorder
    from sqlalchemy.orm import Session

_LOGGER = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"[a-z0-9_]+")


@dataclass(slots=True)
class PurgeOutcome:
    """One deleted history and what was (or, for a dry run, would be) deleted."""

    entity_id: str
    applied: bool
    deleted_states: int = 0
    deleted_statistics: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for the service response (JSON-serialisable)."""
        return asdict(self)


@dataclass(slots=True)
class DeleteReport:
    """What a targeted delete did, plus the selection parts that matched nothing."""

    outcomes: list[PurgeOutcome]
    not_found_entity_ids: list[str]
    not_found_domains: list[str]


def valid_delete_domain(raw: str) -> bool:
    """Whether a raw domain input is acceptable to the delete selection.

    Blank input is acceptable (it is simply ignored); anything else must
    normalise to a plain domain slug like ``sensor``.
    """
    domain = raw.strip().lower().removesuffix(".")
    return not domain or _DOMAIN_RE.fullmatch(domain) is not None


def normalise_delete_targets(
    entity_ids: Iterable[str], domains: Iterable[str]
) -> tuple[set[str], set[str]]:
    """Strip and validate a targeted-delete selection.

    Entity ids are only stripped, never case-folded or shape-checked: they are
    matched exactly as stored, so junk ids that could never be typed through a
    validator can still be cleaned up. Domains are lower-cased (a forgiving
    trailing dot is dropped) and must be plain domain slugs.
    """
    ids = {entity_id.strip() for entity_id in entity_ids} - {""}
    normalised_domains: set[str] = set()
    for raw in domains:
        if not valid_delete_domain(raw):
            raise ServiceValidationError(
                f"Not a valid domain: '{raw.strip()}'. Use a plain domain like"
                " 'sensor' — specific ids belong in the entity ids list."
            )
        if domain := raw.strip().lower().removesuffix("."):
            normalised_domains.add(domain)
    if not ids and not normalised_domains:
        raise ServiceValidationError(
            "Provide at least one entity id or domain whose history to delete."
        )
    return ids, normalised_domains


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
    task = _PurgeOrphansTask(dry_run=dry_run, repack=repack, restrict_to=restrict_to)
    await _wait_for_task(hass, task, "purging orphaned history")
    return task.outcomes


async def async_delete_history(
    hass: HomeAssistant,
    *,
    entity_ids: Iterable[str] = (),
    domains: Iterable[str] = (),
    dry_run: bool = False,
    repack: bool = False,
    restrict_to: set[str] | None = None,
) -> DeleteReport:
    """Delete the whole history of the named entity ids and/or domains.

    The selection is explicit — live entities included (they re-create fresh
    metadata on their next recorded state). A dry run only reports; the report
    also names selection parts that matched nothing, so typos surface in the
    preview. ``restrict_to`` and ``repack`` behave as in
    ``async_purge_orphans``. Raises ``ServiceValidationError`` for an empty
    or invalid selection.
    """
    ids, normalised_domains = normalise_delete_targets(entity_ids, domains)
    task = _DeleteHistoryTask(
        entity_ids=ids,
        domains=normalised_domains,
        dry_run=dry_run,
        repack=repack,
        restrict_to=restrict_to,
    )
    await _wait_for_task(hass, task, "deleting history")
    return task.report


async def async_repack_database(hass: HomeAssistant) -> None:
    """Repack the database without deleting anything.

    The same repack ``recorder.purge``'s repack option runs — for reclaiming
    the space of deletions that were applied without it (by this integration,
    core purges, or anything else).
    """
    task = _RepackTask()
    await _wait_for_task(hass, task, "repacking the database")


async def _wait_for_task(
    hass: HomeAssistant,
    task: _PurgeOrphansTask | _DeleteHistoryTask | _RepackTask,
    doing: str,
) -> None:
    """Queue a task on the recorder thread and wait for it to finish."""
    get_instance(hass).queue_task(task)
    finished = await hass.async_add_executor_job(task.done.wait, RECORDER_TASK_TIMEOUT)
    if not finished:
        raise HomeAssistantError(
            f"Timed out waiting for the recorder to finish {doing}. The queued"
            " operation (and repack, if requested) may still complete in the"
            " background — check the log for what was applied before retrying."
        )
    if task.error is not None:
        raise HomeAssistantError(
            f"History Mover failed while {doing}: {task.error}"
        ) from task.error


def _maybe_repack(instance: Recorder, repack: bool, dry_run: bool) -> None:
    """After a committed delete, reclaim the freed space if asked to.

    The same repack ``recorder.purge``'s own repack option runs. Done even
    when nothing matched: the user asked for a repack.
    """
    if repack and not dry_run:
        _LOGGER.info("Repacking the database to reclaim freed space")
        repack_database(instance)


@dataclass(slots=True)
class _PurgeOrphansTask(RecorderTask):
    """Runs the whole orphan purge on the recorder thread and signals completion.

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
            _maybe_repack(instance, self.repack, self.dry_run)
        except Exception as err:  # surfaced to the caller via .error
            _LOGGER.exception("History Mover purge task failed")
            self.error = err
        finally:
            self.done.set()


@dataclass(slots=True)
class _DeleteHistoryTask(RecorderTask):
    """Runs one targeted delete on the recorder thread and signals completion."""

    entity_ids: set[str]
    domains: set[str]
    dry_run: bool
    repack: bool
    restrict_to: set[str] | None
    report: DeleteReport = field(default_factory=lambda: DeleteReport([], [], []))
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def run(self, instance: Recorder) -> None:
        try:
            self.report = _run_delete(
                instance, self.entity_ids, self.domains, self.dry_run, self.restrict_to
            )
            _maybe_repack(instance, self.repack, self.dry_run)
        except Exception as err:  # surfaced to the caller via .error
            _LOGGER.exception("History Mover delete task failed")
            self.error = err
        finally:
            self.done.set()


@dataclass(slots=True)
class _RepackTask(RecorderTask):
    """Runs a bare repack on the recorder thread and signals completion."""

    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def run(self, instance: Recorder) -> None:
        try:
            _maybe_repack(instance, repack=True, dry_run=False)
        except Exception as err:  # surfaced to the caller via .error
            _LOGGER.exception("History Mover repack task failed")
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

    with session_scope(session=instance.get_session()) as session:
        states_by_id, stats_by_id = _meta_maps(session)
        orphan_ids = {
            entity_id
            for entity_id in set(states_by_id) | set(stats_by_id)
            if entity_id not in alive
        }
        if restrict_to is not None:
            orphan_ids &= restrict_to
        outcomes, purged_ids, removed_attributes = _delete_ids(
            instance, session, orphan_ids, states_by_id, stats_by_id, dry_run
        )

    _log_outcomes(
        outcomes,
        "Purged orphaned history %s: deleted %d states / %d statistics rows",
        "Would purge orphaned history %s: %d states / %d statistics rows",
    )
    _evict_caches(instance, purged_ids, removed_attributes)
    return outcomes


def _run_delete(
    instance: Recorder,
    entity_ids: set[str],
    domains: set[str],
    dry_run: bool,
    restrict_to: set[str] | None,
) -> DeleteReport:
    """Match the selection and (unless dry_run) delete it in one transaction."""
    with session_scope(session=instance.get_session()) as session:
        states_by_id, stats_by_id = _meta_maps(session)
        all_ids = set(states_by_id) | set(stats_by_id)
        matched = {
            entity_id
            for entity_id in all_ids
            if entity_id in entity_ids or entity_id.split(".", 1)[0] in domains
        }
        if restrict_to is not None:
            matched &= restrict_to
        outcomes, purged_ids, removed_attributes = _delete_ids(
            instance, session, matched, states_by_id, stats_by_id, dry_run
        )

    _log_outcomes(
        outcomes,
        "Deleted history %s: %d states / %d statistics rows",
        "Would delete history %s: %d states / %d statistics rows",
    )
    _evict_caches(instance, purged_ids, removed_attributes)
    domains_present = {entity_id.split(".", 1)[0] for entity_id in all_ids}
    return DeleteReport(
        outcomes=outcomes,
        not_found_entity_ids=sorted(entity_ids - all_ids),
        not_found_domains=sorted(domains - domains_present),
    )


def _meta_maps(session: Session) -> tuple[dict[str, int], dict[str, int]]:
    """Every deletable id: ``{entity_id: metadata_id}`` per stream kind.

    For statistics that is recorder-sourced, entity-shaped ids only
    (``domain.object``) — external statistics (``domain:object``) have no
    entity by design and are never candidates.
    """
    states_by_id = {
        entity_id: metadata_id
        for metadata_id, entity_id in session.query(
            StatesMeta.metadata_id, StatesMeta.entity_id
        )
        if entity_id
    }
    stats_by_id = {
        statistic_id: meta_id
        for meta_id, statistic_id in session.query(
            StatisticsMeta.id, StatisticsMeta.statistic_id
        ).filter(StatisticsMeta.source == "recorder")
        if statistic_id and "." in statistic_id and ":" not in statistic_id
    }
    return states_by_id, stats_by_id


def _delete_ids(
    instance: Recorder,
    session: Session,
    ids: set[str],
    states_by_id: dict[str, int],
    stats_by_id: dict[str, int],
    dry_run: bool,
) -> tuple[list[PurgeOutcome], set[str], set[int]]:
    """Count and (unless dry_run) delete both streams of every given id.

    Returns the outcomes (sorted by id), the ids actually deleted, and the
    attribute rows removed by the shared-attributes cleanup — the caller
    evicts both from the recorder caches after the transaction commits.
    """
    outcomes: list[PurgeOutcome] = []
    purged_ids: set[str] = set()
    attributes_ids: set[int] = set()
    for entity_id in sorted(ids):
        states_meta_id = states_by_id.get(entity_id)
        stats_meta_id = stats_by_id.get(entity_id)
        outcomes.append(
            PurgeOutcome(
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
        )
        if dry_run:
            continue
        if states_meta_id is not None:
            attributes_ids |= discard_states(session, states_meta_id)
        if stats_meta_id is not None:
            discard_statistics(session, stats_meta_id)
        purged_ids.add(entity_id)
    removed_attributes: set[int] = set()
    if attributes_ids:
        # One pass over everything the deleted states referenced; only rows
        # no surviving state shares are dropped (see db.py).
        removed_attributes = delete_unused_attributes(
            instance, session, attributes_ids
        )
    return outcomes, purged_ids, removed_attributes


def _log_outcomes(
    outcomes: list[PurgeOutcome], applied_template: str, dry_run_template: str
) -> None:
    """Log only after the transaction committed — these lines promise durable
    changes (a failed run is logged by the task's error handler instead)."""
    for outcome in outcomes:
        if outcome.applied:
            _LOGGER.info(
                applied_template,
                outcome.entity_id,
                outcome.deleted_states,
                outcome.deleted_statistics,
            )
        else:
            _LOGGER.debug(
                dry_run_template,
                outcome.entity_id,
                outcome.deleted_states,
                outcome.deleted_statistics,
            )


def _evict_caches(
    instance: Recorder, purged_ids: set[str], removed_attributes: set[int]
) -> None:
    """Post-commit, recorder-thread cache fixes, exactly like the mover.

    A deleted id that records again (a live entity whose history was deleted,
    or an orphan coming back to life) must resolve fresh metadata, not a
    cached id pointing at deleted rows.
    """
    if purged_ids:
        instance.states_meta_manager.evict_purged(purged_ids)
        instance.states_manager.evict_purged_entity_ids(purged_ids)
        instance.statistics_meta_manager.reset()
    if removed_attributes:
        instance.state_attributes_manager.evict_purged(removed_attributes)
        _LOGGER.debug(
            "Deleted %d unused shared attribute rows", len(removed_attributes)
        )
