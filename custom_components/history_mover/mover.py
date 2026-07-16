"""The recorder history-adoption engine.

Home Assistant already migrates a registered entity's history when you rename
it through the UI to a *free* entity id — the recorder listens for the entity
registry update and re-labels ``states_meta`` and ``statistics_meta`` (see
``homeassistant/components/recorder/entity_registry.py``). It refuses, though,
when the new id already holds history: ``states_meta_manager.update_metadata``
returns ``False`` and logs *"already in use"*.

This module fills that gap. It moves the recorder history of a *source* id onto
a *target* id, discarding whatever colliding history the target already has —
the "replace" the built-in rename won't do. That is what lets a replacement
integration (say a new PV inverter integration) adopt the long history of the
one it supersedes and keep recording into it.

Why it works, and why it is safe:

* Nothing in ``states``/``statistics`` stores the entity id — those tables
  reference a numeric ``metadata_id``. Moving history is therefore just
  re-labelling the one ``states_meta`` / ``statistics_meta`` row, plus deleting
  the target's own (discarded) rows. No per-state rewrite, any row count.
* It runs entirely through the recorder's own SQLAlchemy session, so SQLite,
  MariaDB/MySQL and PostgreSQL are all handled by one code path.
* It runs as a ``RecorderTask`` on the recorder thread. That is the only thread
  allowed to touch the in-memory ``entity_id -> metadata_id`` caches, and
  ``commit_before`` flushes any in-flight states before we read counts, so we
  never race live recording.
* After committing, it evicts the moved ids from those caches. The live target
  then re-resolves to the adopted ``metadata_id`` on its next recorded state and
  continues the history seamlessly — no restart needed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import valid_entity_id
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from sqlalchemy import func

from .const import (
    CONFLICT_FAIL,
    CONFLICT_SKIP,
    DEFAULT_ON_CONFLICT,
    RECORDER_TASK_TIMEOUT,
    STATUS_FAILED,
    STATUS_NOOP,
    STATUS_RENAMED,
    STATUS_REPLACED,
    STATUS_SKIPPED,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.components.recorder import Recorder
    from homeassistant.core import HomeAssistant
    from sqlalchemy.orm import Session

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RenameRequest:
    """One source id whose history should move onto one target id."""

    old_entity_id: str
    new_entity_id: str


@dataclass(slots=True)
class RenameOutcome:
    """What happened (or, for a dry run, what would happen) to one pair."""

    old_entity_id: str
    new_entity_id: str
    status: str
    applied: bool
    moved_states: int = 0
    discarded_states: int = 0
    moved_statistics: int = 0
    discarded_statistics: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Plain dict for the service response (JSON-serialisable)."""
        return {
            "old_entity_id": self.old_entity_id,
            "new_entity_id": self.new_entity_id,
            "status": self.status,
            "applied": self.applied,
            "moved_states": self.moved_states,
            "discarded_states": self.discarded_states,
            "moved_statistics": self.moved_statistics,
            "discarded_statistics": self.discarded_statistics,
            "detail": self.detail,
        }


async def async_list_history_ids(hass: HomeAssistant, prefix: str) -> list[str]:
    """Recorder ids (states and entity statistics) that start with ``prefix``.

    Includes ids no longer in the entity registry — the orphaned histories that
    bulk migration most often targets. Runs in the recorder's executor.
    """
    return await hass.async_add_executor_job(_list_history_ids, hass, prefix)


def _list_history_ids(hass: HomeAssistant, prefix: str) -> list[str]:
    instance = get_instance(hass)
    # Escape LIKE wildcards so an entity id's underscores are matched literally.
    safe = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"{safe}%"
    ids: set[str] = set()
    with session_scope(session=instance.get_session()) as session:
        for (entity_id,) in session.query(StatesMeta.entity_id).filter(
            StatesMeta.entity_id.like(pattern, escape="\\")
        ):
            if entity_id:
                ids.add(entity_id)
        for (statistic_id,) in session.query(StatisticsMeta.statistic_id).filter(
            StatisticsMeta.statistic_id.like(pattern, escape="\\"),
            StatisticsMeta.source == "recorder",
        ):
            # Only entity-shaped ids (domain.object), never domain:external ones.
            if statistic_id and "." in statistic_id and ":" not in statistic_id:
                ids.add(statistic_id)
    return sorted(ids)


def _validate_requests(requests: list[RenameRequest]) -> None:
    """Reject batches whose outcome would be ambiguous or order-dependent.

    Every id must be a distinct source moving to a distinct, unrelated target.
    An id on both sides (a swap or a chain) would make the result depend on
    processing order — and, with ``replace``, silently destroy history.

    Targets must be structurally valid entity ids: live recording only ever
    writes lower-case ``domain.object_id`` ids, so relabelling history to
    anything else would strand it where nothing can record into it — or address
    it again. Sources are only looked up, so they stay unrestricted (an invalid
    source is a harmless noop, and a permissive lookup keeps a rescue path for
    ids that should not exist).
    """
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    for req in requests:
        if not valid_entity_id(req.new_entity_id):
            raise ServiceValidationError(
                f"Target is not a valid entity id: {req.new_entity_id}"
            )
        if req.old_entity_id == req.new_entity_id:
            raise ServiceValidationError(
                f"Source and target are the same id: {req.old_entity_id}"
            )
        if req.old_entity_id in seen_old:
            raise ServiceValidationError(
                f"The same source appears twice in one call: {req.old_entity_id}"
            )
        if req.new_entity_id in seen_new:
            raise ServiceValidationError(
                f"The same target appears twice in one call: {req.new_entity_id}"
            )
        seen_old.add(req.old_entity_id)
        seen_new.add(req.new_entity_id)
    if overlap := seen_old & seen_new:
        raise ServiceValidationError(
            f"The same id appears as both a source and a target in one call: "
            f"{sorted(overlap)[0]}. The outcome would depend on processing "
            "order — split this into separate calls."
        )


async def async_move_history(
    hass: HomeAssistant,
    requests: Iterable[RenameRequest],
    *,
    on_conflict: str = DEFAULT_ON_CONFLICT,
    dry_run: bool = False,
) -> list[RenameOutcome]:
    """Move history for each request; return one outcome per request, in order.

    Queues a single task on the recorder thread and waits for it. A dry run
    reads counts and reports the decision without changing anything. Raises
    ``ServiceValidationError`` for a batch whose ids collide (see
    ``_validate_requests``).
    """
    request_list = list(requests)
    _validate_requests(request_list)
    instance = get_instance(hass)
    task = _RenameHistoryTask(
        requests=request_list, on_conflict=on_conflict, dry_run=dry_run
    )
    instance.queue_task(task)
    finished = await hass.async_add_executor_job(task.done.wait, RECORDER_TASK_TIMEOUT)
    if not finished:
        raise HomeAssistantError(
            "Timed out waiting for the recorder to finish moving history."
        )
    if task.error is not None:
        raise HomeAssistantError(
            f"History Mover failed while moving history: {task.error}"
        ) from task.error
    return task.outcomes


@dataclass(slots=True)
class _RenameHistoryTask(RecorderTask):
    """Runs the whole batch on the recorder thread and signals completion.

    ``commit_before`` (inherited, True) makes the recorder flush pending states
    before ``run`` — so counts are exact and no write for the source/target is
    still in the queue behind us.
    """

    requests: list[RenameRequest]
    on_conflict: str
    dry_run: bool
    outcomes: list[RenameOutcome] = field(default_factory=list)
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def run(self, instance: Recorder) -> None:
        try:
            self.outcomes = _run_batch(
                instance, self.requests, self.on_conflict, self.dry_run
            )
        except Exception as err:  # surfaced to the caller via .error
            _LOGGER.exception("History Mover rename task failed")
            self.error = err
        finally:
            self.done.set()


def _run_batch(
    instance: Recorder,
    requests: list[RenameRequest],
    on_conflict: str,
    dry_run: bool,
) -> list[RenameOutcome]:
    """Process every pair in one transaction, then fix the caches."""
    outcomes: list[RenameOutcome] = []
    touched: set[str] = set()
    with session_scope(session=instance.get_session()) as session:
        for req in requests:
            outcome = _process_pair(session, req, on_conflict, dry_run)
            outcomes.append(outcome)
            if outcome.applied:
                touched.add(req.old_entity_id)
                touched.add(req.new_entity_id)

    # Only after the writes are committed, and only on this (recorder) thread,
    # fix the in-memory caches so live recording continues into the adopted
    # history. Three caches key off the entity id and would otherwise be stale:
    if touched:
        # 1. entity_id -> metadata_id: drop the moved ids so the next lookup
        #    re-reads them; the target then resolves to the adopted metadata.
        instance.states_meta_manager.evict_purged(touched)
        # 2. entity_id -> last state_id (for old_state_id linking): the target's
        #    remembered last state was just deleted, and the source's now belongs
        #    to the target. Evicting both starts each next state on a fresh link,
        #    exactly as purge does (recorder/purge.py). Without this the first
        #    post-move state would reference a deleted row.
        instance.states_manager.evict_purged_entity_ids(touched)
        # 3. statistic_id -> metadata cache: no per-id evict, so reset it. Cheap
        #    (it re-populates lazily) and keeps this simple and correct.
        instance.statistics_meta_manager.reset()
    return outcomes


def _process_pair(
    session: Session,
    req: RenameRequest,
    on_conflict: str,
    dry_run: bool,
) -> RenameOutcome:
    """Decide and (unless dry_run) apply the move for one source/target pair.

    States and statistics are moved independently: a stream is only touched
    when the source actually has it, and the target's matching stream is only
    discarded when the source is about to replace it. So renaming an entity that
    only has statistics never disturbs an unrelated states history on the target.
    """
    old = req.old_entity_id
    new = req.new_entity_id

    src_meta = _states_meta(session, old)
    dst_meta = _states_meta(session, new)
    src_stat = _statistics_meta(session, old)
    dst_stat = _statistics_meta(session, new)

    has_src_states = src_meta is not None
    has_src_stats = src_stat is not None
    if not has_src_states and not has_src_stats:
        return RenameOutcome(
            old, new, STATUS_NOOP, applied=False,
            detail="Source has no recorder history to move.",
        )

    # A collision only counts on a stream the source will actually move.
    states_collision = has_src_states and dst_meta is not None
    stats_collision = has_src_stats and dst_stat is not None
    collision = states_collision or stats_collision

    if collision and on_conflict == CONFLICT_FAIL:
        return RenameOutcome(
            old, new, STATUS_FAILED, applied=False,
            detail="Target already holds history and on_conflict is 'fail'.",
        )
    if collision and on_conflict == CONFLICT_SKIP:
        return RenameOutcome(
            old, new, STATUS_SKIPPED, applied=False,
            detail="Target already holds history and on_conflict is 'skip'.",
        )

    # Counts first — before any row is moved or deleted.
    moved_states = _count(session, States, src_meta.metadata_id) if src_meta else 0
    discarded_states = _count(session, States, dst_meta.metadata_id) if states_collision and dst_meta else 0
    moved_stats = _count_statistics(session, src_stat.id) if src_stat else 0
    discarded_stats = _count_statistics(session, dst_stat.id) if stats_collision and dst_stat else 0

    status = STATUS_REPLACED if collision else STATUS_RENAMED

    if dry_run:
        return RenameOutcome(
            old, new, status, applied=False,
            moved_states=moved_states, discarded_states=discarded_states,
            moved_statistics=moved_stats, discarded_statistics=discarded_stats,
            detail="Preview only — nothing was changed.",
        )

    if src_meta is not None:
        if states_collision and dst_meta is not None:
            _discard_states(session, dst_meta.metadata_id)
        _relabel_states(session, src_meta.metadata_id, new)
    if src_stat is not None:
        if stats_collision and dst_stat is not None:
            _discard_statistics(session, dst_stat.id)
        _relabel_statistics(session, src_stat.id, new)

    return RenameOutcome(
        old, new, status, applied=True,
        moved_states=moved_states, discarded_states=discarded_states,
        moved_statistics=moved_stats, discarded_statistics=discarded_stats,
        detail=(
            "Adopted the source history; the target's own history was discarded."
            if collision
            else "Target id was free; history moved over unchanged."
        ),
    )


def _states_meta(session: Session, entity_id: str) -> StatesMeta | None:
    return (
        session.query(StatesMeta)
        .filter(StatesMeta.entity_id == entity_id)
        .one_or_none()
    )


def _statistics_meta(session: Session, statistic_id: str) -> StatisticsMeta | None:
    # statistic_id is unique; an entity id (``sensor.x``) can only match the
    # recorder's own entity statistics, never a ``domain:external`` id.
    return (
        session.query(StatisticsMeta)
        .filter(StatisticsMeta.statistic_id == statistic_id)
        .one_or_none()
    )


def _count(session: Session, model: type[States], metadata_id: int) -> int:
    result = (
        session.query(func.count())
        .select_from(model)
        .filter(model.metadata_id == metadata_id)
        .scalar()
    )
    return int(result or 0)


def _count_statistics(session: Session, metadata_id: int) -> int:
    total = 0
    for model in (Statistics, StatisticsShortTerm):
        result = (
            session.query(func.count())
            .select_from(model)
            .filter(model.metadata_id == metadata_id)
            .scalar()
        )
        total += int(result or 0)
    return total


def _relabel_states(session: Session, metadata_id: int, new_entity_id: str) -> None:
    session.query(StatesMeta).filter(StatesMeta.metadata_id == metadata_id).update(
        {StatesMeta.entity_id: new_entity_id}, synchronize_session=False
    )


def _relabel_statistics(session: Session, metadata_id: int, new_statistic_id: str) -> None:
    session.query(StatisticsMeta).filter(StatisticsMeta.id == metadata_id).update(
        {StatisticsMeta.statistic_id: new_statistic_id}, synchronize_session=False
    )


def _discard_states(session: Session, metadata_id: int) -> None:
    """Delete every state row (and the meta row) for a metadata id."""
    # Null the self-referential old_state_id chain first, so the bulk delete
    # can't trip the states.old_state_id -> states.state_id foreign key on
    # databases that enforce it.
    session.query(States).filter(States.metadata_id == metadata_id).update(
        {States.old_state_id: None}, synchronize_session=False
    )
    session.query(States).filter(States.metadata_id == metadata_id).delete(
        synchronize_session=False
    )
    session.query(StatesMeta).filter(StatesMeta.metadata_id == metadata_id).delete(
        synchronize_session=False
    )


def _discard_statistics(session: Session, metadata_id: int) -> None:
    """Delete long-term + short-term statistics (and the meta row) for a metadata id."""
    session.query(StatisticsShortTerm).filter(
        StatisticsShortTerm.metadata_id == metadata_id
    ).delete(synchronize_session=False)
    session.query(Statistics).filter(Statistics.metadata_id == metadata_id).delete(
        synchronize_session=False
    )
    session.query(StatisticsMeta).filter(StatisticsMeta.id == metadata_id).delete(
        synchronize_session=False
    )
