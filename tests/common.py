"""Shared helpers for History Mover tests: record/insert and count DB history."""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

# A base timestamp for inserted statistics rows (2020-09-13T12:26:40Z).
_STATS_START = 1_600_000_000.0


async def record_states(
    hass: HomeAssistant,
    entity_id: str,
    values: Sequence[str],
    *,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    """Set a series of distinct states and wait for them to be recorded."""
    for value in values:
        hass.states.async_set(entity_id, value, attributes)
    await async_wait_recording_done(hass)


async def remove_entity(hass: HomeAssistant, entity_id: str) -> None:
    """Remove the live state so only the recorded history remains (an orphan).

    Note the recorder records the removal itself as one final NULL-state row,
    so an entity recorded with N values holds N+1 rows afterwards.
    """
    hass.states.async_remove(entity_id)
    await async_wait_recording_done(hass)


def _count_states(hass: HomeAssistant, entity_id: str) -> int | None:
    instance = get_instance(hass)
    with session_scope(session=instance.get_session()) as session:
        meta = (
            session.query(StatesMeta)
            .filter(StatesMeta.entity_id == entity_id)
            .one_or_none()
        )
        if meta is None:
            return None
        return (
            session.query(States)
            .filter(States.metadata_id == meta.metadata_id)
            .count()
        )


async def count_states(hass: HomeAssistant, entity_id: str) -> int | None:
    """Rows in ``states`` for an entity id, or None if it has no states_meta row."""
    return await hass.async_add_executor_job(_count_states, hass, entity_id)


def _add_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    values: Sequence[float],
    short_term: bool,
    source: str,
) -> None:
    instance = get_instance(hass)
    model = StatisticsShortTerm if short_term else Statistics
    with session_scope(session=instance.get_session()) as session:
        meta = (
            session.query(StatisticsMeta)
            .filter(StatisticsMeta.statistic_id == statistic_id)
            .one_or_none()
        )
        if meta is None:
            meta = StatisticsMeta(
                statistic_id=statistic_id,
                source=source,
                unit_of_measurement="W",
                has_mean=False,
                has_sum=True,
                name=None,
                mean_type=StatisticMeanType.NONE,
            )
            session.add(meta)
            session.flush()
        for index, value in enumerate(values):
            session.add(
                model(
                    metadata_id=meta.id,
                    start_ts=_STATS_START + index * 3600,
                    state=float(value),
                    sum=float(value),
                )
            )


async def add_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    values: Sequence[float],
    *,
    short_term: bool = False,
    source: str = "recorder",
) -> None:
    """Insert statistics rows (and a meta row; recorder-source by default)."""
    await hass.async_add_executor_job(
        functools.partial(
            _add_statistics, hass, statistic_id, values, short_term, source
        )
    )


def _add_states_meta_only(hass: HomeAssistant, entity_id: str) -> None:
    with session_scope(session=get_instance(hass).get_session()) as session:
        session.add(StatesMeta(entity_id=entity_id))


async def add_states_meta_only(hass: HomeAssistant, entity_id: str) -> None:
    """Insert a bare states_meta row with no state rows (a meta-only leftover)."""
    await hass.async_add_executor_job(_add_states_meta_only, hass, entity_id)


def _attribute_payloads(hass: HomeAssistant) -> list[str]:
    with session_scope(session=get_instance(hass).get_session()) as session:
        return [
            shared_attrs or ""
            for (shared_attrs,) in session.query(StateAttributes.shared_attrs)
        ]


async def attribute_payloads(hass: HomeAssistant) -> list[str]:
    """The shared_attrs payload of every state_attributes row in the database."""
    return await hass.async_add_executor_job(_attribute_payloads, hass)


def _count_statistics(hass: HomeAssistant, statistic_id: str) -> int | None:
    instance = get_instance(hass)
    with session_scope(session=instance.get_session()) as session:
        meta = (
            session.query(StatisticsMeta)
            .filter(StatisticsMeta.statistic_id == statistic_id)
            .one_or_none()
        )
        if meta is None:
            return None
        long_term = (
            session.query(Statistics).filter(Statistics.metadata_id == meta.id).count()
        )
        short = (
            session.query(StatisticsShortTerm)
            .filter(StatisticsShortTerm.metadata_id == meta.id)
            .count()
        )
        return long_term + short


async def count_statistics(hass: HomeAssistant, statistic_id: str) -> int | None:
    """Rows in both statistics tables for an id, or None if it has no meta row."""
    return await hass.async_add_executor_job(_count_statistics, hass, statistic_id)
