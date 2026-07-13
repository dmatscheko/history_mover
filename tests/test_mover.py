"""End-to-end tests for the recorder history-adoption engine."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.history_mover.const import (
    CONFLICT_FAIL,
    CONFLICT_SKIP,
    STATUS_FAILED,
    STATUS_NOOP,
    STATUS_RENAMED,
    STATUS_REPLACED,
    STATUS_SKIPPED,
)
from custom_components.history_mover.mover import (
    RenameRequest,
    async_list_history_ids,
    async_move_history,
)

from .common import (
    add_statistics,
    count_states,
    count_statistics,
    record_states,
)


async def test_replace_states_and_continue_recording(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The target adopts the source's states, its own are discarded, and — the
    whole point — the live target keeps recording into the adopted history
    without a restart."""
    await record_states(hass, "sensor.old", ["1", "2", "3"])
    await record_states(hass, "sensor.new", ["100", "200"])
    assert await count_states(hass, "sensor.old") == 3
    assert await count_states(hass, "sensor.new") == 2

    outcomes = await async_move_history(
        hass, [RenameRequest("sensor.old", "sensor.new")]
    )
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status == STATUS_REPLACED
    assert outcome.applied is True
    assert outcome.moved_states == 3
    assert outcome.discarded_states == 2

    # The source id no longer exists in the recorder; the target owns the 3.
    assert await count_states(hass, "sensor.old") is None
    assert await count_states(hass, "sensor.new") == 3

    # Continue: a fresh state on the live target appends to the adopted history.
    await record_states(hass, "sensor.new", ["4"])
    assert await count_states(hass, "sensor.new") == 4


async def test_rename_onto_free_target(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A free target is a plain rename — history moves over, nothing discarded."""
    await record_states(hass, "sensor.old", ["1", "2", "3"])

    outcomes = await async_move_history(
        hass, [RenameRequest("sensor.old", "sensor.brand_new")]
    )
    assert outcomes[0].status == STATUS_RENAMED
    assert outcomes[0].moved_states == 3
    assert outcomes[0].discarded_states == 0
    assert await count_states(hass, "sensor.old") is None
    assert await count_states(hass, "sensor.brand_new") == 3


async def test_move_statistics(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Long-term and short-term statistics follow the rename; the target's are
    discarded on replace."""
    await add_statistics(hass, "sensor.old", [1.0, 2.0, 3.0])
    await add_statistics(hass, "sensor.old", [1.0, 2.0], short_term=True)
    await add_statistics(hass, "sensor.new", [9.0])
    assert await count_statistics(hass, "sensor.old") == 5  # 3 long + 2 short
    assert await count_statistics(hass, "sensor.new") == 1

    outcomes = await async_move_history(
        hass, [RenameRequest("sensor.old", "sensor.new")]
    )
    assert outcomes[0].status == STATUS_REPLACED
    assert outcomes[0].moved_statistics == 5
    assert outcomes[0].discarded_statistics == 1
    assert await count_statistics(hass, "sensor.old") is None
    assert await count_statistics(hass, "sensor.new") == 5


async def test_orphaned_source_with_only_statistics(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A source that only has statistics (no states, e.g. a removed integration)
    still moves, and an unrelated states history on the target is left alone."""
    await add_statistics(hass, "sensor.gone", [1.0, 2.0])
    await record_states(hass, "sensor.live", ["10", "20", "30"])

    outcomes = await async_move_history(
        hass, [RenameRequest("sensor.gone", "sensor.live")]
    )
    # The target has no statistics of its own, so moving the source's onto it is
    # a free rename on the statistics stream — nothing collides, nothing discarded.
    assert outcomes[0].status == STATUS_RENAMED
    assert outcomes[0].moved_statistics == 2
    assert outcomes[0].discarded_statistics == 0
    # The source had no states, so the target keeps its own states untouched.
    assert outcomes[0].moved_states == 0
    assert outcomes[0].discarded_states == 0
    assert await count_states(hass, "sensor.live") == 3
    assert await count_statistics(hass, "sensor.live") == 2


async def test_noop_when_source_has_no_history(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    outcomes = await async_move_history(
        hass, [RenameRequest("sensor.nothing", "sensor.target")]
    )
    assert outcomes[0].status == STATUS_NOOP
    assert outcomes[0].applied is False


async def test_on_conflict_skip_and_fail(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await record_states(hass, "sensor.old", ["1", "2"])
    await record_states(hass, "sensor.new", ["9"])

    skipped = await async_move_history(
        hass, [RenameRequest("sensor.old", "sensor.new")], on_conflict=CONFLICT_SKIP
    )
    assert skipped[0].status == STATUS_SKIPPED
    assert skipped[0].applied is False
    assert await count_states(hass, "sensor.old") == 2  # untouched
    assert await count_states(hass, "sensor.new") == 1

    failed = await async_move_history(
        hass, [RenameRequest("sensor.old", "sensor.new")], on_conflict=CONFLICT_FAIL
    )
    assert failed[0].status == STATUS_FAILED
    assert failed[0].applied is False
    assert await count_states(hass, "sensor.old") == 2


async def test_dry_run_changes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await record_states(hass, "sensor.old", ["1", "2", "3"])
    await record_states(hass, "sensor.new", ["9"])

    outcomes = await async_move_history(
        hass, [RenameRequest("sensor.old", "sensor.new")], dry_run=True
    )
    assert outcomes[0].status == STATUS_REPLACED
    assert outcomes[0].applied is False
    assert outcomes[0].moved_states == 3
    assert outcomes[0].discarded_states == 1
    # Nothing actually moved.
    assert await count_states(hass, "sensor.old") == 3
    assert await count_states(hass, "sensor.new") == 1


async def test_bulk_rename(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    await record_states(hass, "sensor.a_old", ["1", "2"])
    await record_states(hass, "sensor.b_old", ["1", "2", "3"])
    await record_states(hass, "sensor.b_new", ["9"])  # occupied target

    outcomes = await async_move_history(
        hass,
        [
            RenameRequest("sensor.a_old", "sensor.a_new"),  # free target
            RenameRequest("sensor.b_old", "sensor.b_new"),  # occupied target
        ],
    )
    assert [o.status for o in outcomes] == [STATUS_RENAMED, STATUS_REPLACED]
    assert await count_states(hass, "sensor.a_new") == 2
    assert await count_states(hass, "sensor.b_new") == 3
    assert await count_states(hass, "sensor.a_old") is None
    assert await count_states(hass, "sensor.b_old") is None


async def test_list_history_ids_covers_states_and_statistics(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Bulk discovery finds ids from both tables, including a statistics-only
    (orphaned) id, and matches the prefix literally (underscores aren't wildcards)."""
    await record_states(hass, "sensor.pref_states", ["1"])
    await add_statistics(hass, "sensor.pref_stats", [1.0, 2.0])
    await record_states(hass, "sensor.other_one", ["1"])  # different prefix

    ids = await async_list_history_ids(hass, "sensor.pref_")
    assert set(ids) == {"sensor.pref_states", "sensor.pref_stats"}
    assert await async_list_history_ids(hass, "sensor.zzz_") == []


async def test_engine_error_surfaces_as_home_assistant_error(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An unexpected failure on the recorder thread is reported to the caller,
    not swallowed."""
    with (
        patch(
            "custom_components.history_mover.mover._run_batch",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(HomeAssistantError, match="failed while moving history"),
    ):
        await async_move_history(hass, [RenameRequest("sensor.x", "sensor.y")])


async def test_timeout_is_reported(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """If the recorder never runs the task, the caller gets a clear timeout."""
    with (
        patch("custom_components.history_mover.mover.RECORDER_TASK_TIMEOUT", 0.05),
        patch.object(get_instance(hass), "queue_task"),  # swallow the task
        pytest.raises(HomeAssistantError, match="Timed out"),
    ):
        await async_move_history(hass, [RenameRequest("sensor.x", "sensor.y")])
