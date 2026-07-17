"""End-to-end tests for the orphan purge engine."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.history_mover.purger import async_purge_orphans

from .common import (
    add_states_meta_only,
    add_statistics,
    attribute_payloads,
    count_states,
    count_statistics,
    record_states,
    remove_entity,
)


async def test_purge_orphan_and_keep_alive(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An id with history but no entity is purged; a live entity never is —
    and a purged id that comes back to life records into fresh metadata."""
    await record_states(hass, "sensor.alive", ["1", "2"])
    await record_states(hass, "sensor.gone", ["1", "2", "3"])
    await remove_entity(hass, "sensor.gone")

    outcomes = await async_purge_orphans(hass)
    assert [o.entity_id for o in outcomes] == ["sensor.gone"]
    assert outcomes[0].applied is True
    # 3 recorded values + the recorded removal (see remove_entity).
    assert outcomes[0].deleted_states == 4
    assert outcomes[0].deleted_statistics == 0
    assert await count_states(hass, "sensor.gone") is None
    assert await count_states(hass, "sensor.alive") == 2

    # The whole point of the cache eviction: the id can come back and record.
    await record_states(hass, "sensor.gone", ["9"])
    assert await count_states(hass, "sensor.gone") == 1


async def test_registry_entry_protects_stateless_entity(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """No current state but still registered (e.g. disabled or not loaded yet)
    means alive — the history stays."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor", "test", "unique_reg", suggested_object_id="reg_gone"
    )
    assert entry.entity_id == "sensor.reg_gone"
    await record_states(hass, "sensor.reg_gone", ["1", "2"])
    await remove_entity(hass, "sensor.reg_gone")

    outcomes = await async_purge_orphans(hass)
    assert outcomes == []
    assert await count_states(hass, "sensor.reg_gone") == 3


async def test_statistics_only_orphan_purged_external_untouched(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A statistics-only orphan (long + short term) is purged; external
    statistics have no entity by design and are never candidates."""
    await add_statistics(hass, "sensor.stats_gone", [1.0, 2.0])
    await add_statistics(hass, "sensor.stats_gone", [1.0], short_term=True)
    await add_statistics(hass, "test:external", [5.0], source="test")

    outcomes = await async_purge_orphans(hass)
    assert [o.entity_id for o in outcomes] == ["sensor.stats_gone"]
    assert outcomes[0].deleted_states == 0
    assert outcomes[0].deleted_statistics == 3
    assert await count_statistics(hass, "sensor.stats_gone") is None
    assert await count_statistics(hass, "test:external") == 1


async def test_meta_only_leftover_is_purged(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A states_meta row with zero state rows is still an orphan (0/0 counts)."""
    await add_states_meta_only(hass, "sensor.meta_only")

    outcomes = await async_purge_orphans(hass)
    assert [o.entity_id for o in outcomes] == ["sensor.meta_only"]
    assert outcomes[0].deleted_states == 0
    assert await count_states(hass, "sensor.meta_only") is None


async def test_dry_run_reports_without_change(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await record_states(hass, "sensor.dry_gone", ["1", "2"])
    await remove_entity(hass, "sensor.dry_gone")
    await add_statistics(hass, "sensor.dry_gone", [1.0])

    outcomes = await async_purge_orphans(hass, dry_run=True)
    assert [o.entity_id for o in outcomes] == ["sensor.dry_gone"]
    assert outcomes[0].applied is False
    assert outcomes[0].deleted_states == 3
    assert outcomes[0].deleted_statistics == 1
    # Nothing actually deleted.
    assert await count_states(hass, "sensor.dry_gone") == 3
    assert await count_statistics(hass, "sensor.dry_gone") == 1


async def test_restrict_to_purges_only_previewed_ids(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The guided flow's contract: apply only what the preview showed."""
    await record_states(hass, "sensor.seen", ["1"])
    await record_states(hass, "sensor.unseen", ["1", "2"])
    await remove_entity(hass, "sensor.seen")
    await remove_entity(hass, "sensor.unseen")

    outcomes = await async_purge_orphans(hass, restrict_to={"sensor.seen"})
    assert [o.entity_id for o in outcomes] == ["sensor.seen"]
    assert await count_states(hass, "sensor.seen") is None
    assert await count_states(hass, "sensor.unseen") == 3


async def test_unused_attributes_are_cleaned_up(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Deleting an orphan's states also drops the shared attribute rows only
    it used — while attributes of surviving states stay."""
    await record_states(
        hass, "sensor.attr_gone", ["1", "2"], attributes={"purge_marker": 1}
    )
    await record_states(
        hass, "sensor.attr_alive", ["1"], attributes={"keep_marker": 2}
    )
    await remove_entity(hass, "sensor.attr_gone")
    payloads = "\n".join(await attribute_payloads(hass))
    assert "purge_marker" in payloads and "keep_marker" in payloads

    await async_purge_orphans(hass)
    payloads = "\n".join(await attribute_payloads(hass))
    assert "purge_marker" not in payloads
    assert "keep_marker" in payloads


async def test_repack_runs_only_on_applied_purge(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """repack_database runs after an applied purge with repack=True — never on
    a dry run, never unrequested."""
    with patch(
        "custom_components.history_mover.purger.repack_database"
    ) as repack:
        await async_purge_orphans(hass, dry_run=True, repack=True)
        repack.assert_not_called()
        await async_purge_orphans(hass, repack=False)
        repack.assert_not_called()
        await async_purge_orphans(hass, repack=True)
        repack.assert_called_once_with(get_instance(hass))


async def test_repack_really_vacuums(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Smoke test: the real repack (VACUUM on SQLite) runs without error."""
    await record_states(hass, "sensor.vac_gone", ["1"])
    await remove_entity(hass, "sensor.vac_gone")

    outcomes = await async_purge_orphans(hass, repack=True)
    assert [o.entity_id for o in outcomes] == ["sensor.vac_gone"]
    assert await count_states(hass, "sensor.vac_gone") is None


async def test_apply_refused_while_starting(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Entities that have not loaded yet would look orphaned — applying is
    refused until Home Assistant is fully started. Dry runs are allowed."""
    await record_states(hass, "sensor.start_gone", ["1"])
    await remove_entity(hass, "sensor.start_gone")

    hass.set_state(CoreState.starting)
    try:
        with pytest.raises(ServiceValidationError, match="fully started"):
            await async_purge_orphans(hass)
        outcomes = await async_purge_orphans(hass, dry_run=True)
        assert [o.entity_id for o in outcomes] == ["sensor.start_gone"]
    finally:
        hass.set_state(CoreState.running)
    assert await count_states(hass, "sensor.start_gone") == 2


async def test_every_outcome_is_logged(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Applied purges log at info (the README promises a trace); dry runs at
    debug."""
    await record_states(hass, "sensor.log_gone", ["1"])
    await remove_entity(hass, "sensor.log_gone")
    logger = "custom_components.history_mover.purger"
    with caplog.at_level(logging.DEBUG, logger=logger):
        await async_purge_orphans(hass, dry_run=True)
        await async_purge_orphans(hass)
    assert (
        "Would purge orphaned history sensor.log_gone: 2 states / 0 statistics"
        in caplog.text
    )
    assert (
        "Purged orphaned history sensor.log_gone: deleted 2 states / 0 statistics"
        in caplog.text
    )


async def test_engine_error_surfaces_as_home_assistant_error(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An unexpected failure on the recorder thread is reported, not swallowed."""
    with (
        patch(
            "custom_components.history_mover.purger._run_purge",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(HomeAssistantError, match="failed while purging"),
    ):
        await async_purge_orphans(hass)


async def test_timeout_is_reported(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """If the recorder never runs the task, the caller gets a clear timeout."""
    with (
        patch("custom_components.history_mover.purger.RECORDER_TASK_TIMEOUT", 0.05),
        patch.object(get_instance(hass), "queue_task"),  # swallow the task
        pytest.raises(HomeAssistantError, match="Timed out"),
    ):
        await async_purge_orphans(hass)
