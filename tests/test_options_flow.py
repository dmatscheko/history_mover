"""Tests for the guided rename options flow (single + bulk)."""

from __future__ import annotations

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
