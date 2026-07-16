"""Tests for the guided rename options flow (single + bulk)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.history_mover.const import DOMAIN

from .common import count_states, record_states


async def _setup_entry(hass: HomeAssistant) -> ConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_single_rename_via_options(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.opt_old", ["1", "2", "3"])
    await record_states(hass, "sensor.opt_new", ["9"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "single"}
    )
    assert result["step_id"] == "single"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": "sensor.opt_old",
            "new_entity_id": "sensor.opt_new",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "confirm"
    summary = result["description_placeholders"]["summary"]
    assert "sensor.opt_old" in summary and "replaced" in summary

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert await count_states(hass, "sensor.opt_new") == 3
    assert await count_states(hass, "sensor.opt_old") is None


async def test_single_normalises_case_and_whitespace(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Ids are normalised exactly like the service's cv.entity_id — otherwise an
    uppercase target would strand the history under an id that live recording
    (always lower-case) can never continue."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.norm_old", ["1", "2"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "single"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": " SENSOR.NORM_OLD ",
            "new_entity_id": "Sensor.Norm_New",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert await count_states(hass, "sensor.norm_new") == 2
    assert await count_states(hass, "sensor.norm_old") is None


async def test_single_rejects_invalid_ids(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "single"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": "not_an_entity",
            "new_entity_id": "sensor.bad target",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "single"
    assert result["errors"] == {
        "old_entity_id": "invalid_entity_id",
        "new_entity_id": "invalid_entity_id",
    }


async def test_single_rejects_same_id(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "single"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_entity_id": "sensor.x",
            "new_entity_id": "sensor.x",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "single"
    assert result["errors"] == {"base": "same_id"}


async def test_bulk_rename_via_options(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.bulkopt_a", ["1", "2"])
    await record_states(hass, "sensor.bulkopt_b", ["1", "2", "3"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "bulk"}
    )
    assert result["step_id"] == "bulk"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_prefix": "sensor.bulkopt_",
            "new_prefix": "sensor.moved_",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert await count_states(hass, "sensor.moved_a") == 2
    assert await count_states(hass, "sensor.moved_b") == 3
    assert await count_states(hass, "sensor.bulkopt_a") is None


async def test_bulk_reports_no_matches(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "bulk"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_prefix": "sensor.nothing_here_",
            "new_prefix": "sensor.x_",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "bulk"
    assert result["errors"] == {"base": "no_matches"}


async def test_apply_failure_shows_form_error_and_allows_retry(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An engine failure while applying keeps the flow (and the cached preview)
    alive with an apply_failed error instead of the generic unknown-error toast."""
    entry = await _setup_entry(hass)
    preview = {"dry_run": True, "renames": []}
    with patch(
        "custom_components.history_mover.config_flow.async_perform_rename",
        side_effect=[preview, HomeAssistantError("recorder timeout"), preview],
    ) as perform:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "single"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "old_entity_id": "sensor.af_old",
                "new_entity_id": "sensor.af_new",
                "on_conflict": "replace",
            },
        )
        assert result["step_id"] == "confirm"  # call 1: the dry-run preview

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )  # call 2: apply fails
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "confirm"
        assert result["errors"] == {"base": "apply_failed"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )  # call 3: retry succeeds
        assert result["type"] is FlowResultType.CREATE_ENTRY
    # Exactly three engine calls: the preview was cached, not re-queried.
    assert perform.call_count == 3


async def _bulk_errors(
    hass: HomeAssistant, entry: ConfigEntry, old_prefix: str, new_prefix: str
) -> dict[str, str]:
    """Submit the bulk form once and return its validation errors."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "bulk"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_prefix": old_prefix,
            "new_prefix": new_prefix,
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "bulk"
    errors: dict[str, str] = result["errors"]
    return errors


async def test_bulk_rejects_empty_source_prefix(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An empty source prefix would match every id in the recorder."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.bulkempty_a", ["1"])
    errors = await _bulk_errors(hass, entry, "   ", "sensor.x_")
    assert errors == {"old_prefix": "empty_prefix"}


async def test_bulk_rejects_invalid_generated_targets(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A domain-less target prefix would strand history under unrecordable ids."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.bulkbad_a", ["1"])
    errors = await _bulk_errors(hass, entry, "sensor.bulkbad_", "x_")
    assert errors == {"new_prefix": "invalid_target"}


async def test_bulk_rejects_overlapping_prefixes(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A target prefix that extends the source prefix remaps some targets onto
    other sources — the order-dependent shape the engine refuses (see B1)."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.bulkov_a", ["1"])
    await record_states(hass, "sensor.bulkov_x_a", ["2"])
    errors = await _bulk_errors(hass, entry, "sensor.bulkov_", "sensor.bulkov_x_")
    assert errors == {"new_prefix": "overlapping"}


async def test_bulk_normalises_prefix_case(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Prefixes are lower-cased like the recorder ids they must match."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.bulkcase_a", ["1", "2"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "bulk"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "old_prefix": " SENSOR.BULKCASE_ ",
            "new_prefix": "SENSOR.CASEMOVED_",
            "on_conflict": "replace",
        },
    )
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert await count_states(hass, "sensor.casemoved_a") == 2
    assert await count_states(hass, "sensor.bulkcase_a") is None
