"""Row-level recorder operations shared by the mover and the purger.

Every function here expects to run on the recorder thread, inside a
``session_scope`` the caller owns — they are the building blocks the two
engines compose into one transaction each.

``states`` rows reference shared, deduplicated ``state_attributes`` rows, so
deleting a states stream is a two-step affair: drop the rows, then delete the
attribute rows nothing else references anymore (the same bookkeeping HA core's
own purge does). ``discard_states`` therefore hands back the attribute ids its
deleted rows used; the caller runs ``delete_unused_attributes`` once over the
union after all deletes, so attributes shared between two deleted streams are
judged against what actually survives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.util.collection import chunked_or_all
from sqlalchemy import func

if TYPE_CHECKING:
    from homeassistant.components.recorder import Recorder
    from sqlalchemy.orm import Session


def count_rows(
    session: Session,
    model: type[States | Statistics | StatisticsShortTerm],
    metadata_id: int,
) -> int:
    """Rows in one table for one metadata id."""
    result = (
        session.query(func.count())
        .select_from(model)
        .filter(model.metadata_id == metadata_id)
        .scalar()
    )
    return int(result or 0)


def count_statistics(session: Session, metadata_id: int) -> int:
    """Long-term plus short-term statistics rows for one metadata id."""
    return sum(
        count_rows(session, model, metadata_id)
        for model in (Statistics, StatisticsShortTerm)
    )


def discard_states(session: Session, metadata_id: int) -> set[int]:
    """Delete every state row (and the meta row) for a metadata id.

    Returns the distinct ``attributes_id``s the deleted rows referenced, for a
    later ``delete_unused_attributes`` pass — the attribute rows themselves may
    still be shared with surviving states and cannot be dropped blindly here.
    """
    attributes_ids: set[int] = {
        attributes_id
        for (attributes_id,) in session.query(States.attributes_id)
        .filter(States.metadata_id == metadata_id, States.attributes_id.isnot(None))
        .distinct()
    }
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
    return attributes_ids


def discard_statistics(session: Session, metadata_id: int) -> None:
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


def delete_unused_attributes(
    instance: Recorder, session: Session, attributes_ids: set[int]
) -> set[int]:
    """Delete the given attribute rows that no remaining state references.

    Returns the ids actually deleted, so the caller can evict them from the
    recorder's shared-attributes cache after the transaction commits. Checked
    in ``max_bind_vars`` chunks like core's purge, against the indexed
    ``states.attributes_id`` column.
    """
    deleted: set[int] = set()
    for chunk in chunked_or_all(attributes_ids, instance.max_bind_vars):
        chunk_set = set(chunk)
        still_used: set[int] = {
            attributes_id
            for (attributes_id,) in session.query(States.attributes_id)
            .filter(States.attributes_id.in_(chunk_set))
            .distinct()
        }
        if unused := chunk_set - still_used:
            session.query(StateAttributes).filter(
                StateAttributes.attributes_id.in_(unused)
            ).delete(synchronize_session=False)
            deleted |= unused
    return deleted
