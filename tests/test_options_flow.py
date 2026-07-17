"""Tests for the guided options flow (single + bulk rename, orphan purge)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.history_mover.config_flow import (
    _format_preview,
    _format_purge_preview,
)
from custom_components.history_mover.const import DOMAIN

from .common import count_states, record_states, remove_entity


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


def _preview_item(index: int, status: str) -> dict[str, object]:
    return {
        "old_entity_id": f"sensor.old_{index}",
        "new_entity_id": f"sensor.new_{index}",
        "status": status,
        "moved_states": 2,
        "moved_statistics": 1,
        "discarded_states": 1 if status == "replaced" else 0,
        "discarded_statistics": 0,
    }


def test_format_preview_caps_large_batches_and_adds_totals() -> None:
    """The README advertises bulk moves of hundreds; the confirm dialog shows
    totals plus the first pairs instead of an unbounded wall of lines."""
    preview = {
        "renames": [
            _preview_item(i, "replaced" if i == 0 else "renamed") for i in range(20)
        ]
    }
    text = _format_preview(preview)
    assert "**20 pairs** (19 renamed, 1 replaced)" in text
    assert "move 40 states / 20 statistics; discard 1 / 0" in text
    assert "… and 5 more pairs" in text
    assert "sensor.old_14" in text  # the 15th listed pair
    assert "sensor.old_15" not in text  # capped after that


def test_format_preview_single_pair_stays_plain() -> None:
    text = _format_preview({"renames": [_preview_item(0, "renamed")]})
    assert "pairs" not in text  # no totals header, no cap line
    assert "`sensor.old_0` → `sensor.new_0`" in text


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


async def test_purge_via_options(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The full guided purge: menu → repack choice → preview → apply."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.optpurge_alive", ["1"])
    await record_states(hass, "sensor.optpurge_gone", ["1", "2"])
    await remove_entity(hass, "sensor.optpurge_gone")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "purge"}
    )
    assert result["step_id"] == "purge"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"repack": False}
    )
    assert result["step_id"] == "purge_confirm"
    summary = result["description_placeholders"]["summary"]
    assert "sensor.optpurge_gone" in summary
    assert "3 states" in summary  # 2 values + the recorded removal
    assert "sensor.optpurge_alive" not in summary
    assert "repacked" not in summary  # repack was not ticked

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert await count_states(hass, "sensor.optpurge_gone") is None
    assert await count_states(hass, "sensor.optpurge_alive") == 1


async def test_purge_reports_no_orphans(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """With nothing orphaned the form re-shows with a clear error."""
    entry = await _setup_entry(hass)
    await record_states(hass, "sensor.optpurge_live", ["1"])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "purge"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"repack": True}
    )
    assert result["step_id"] == "purge"
    assert result["errors"] == {"base": "no_orphans"}


async def test_purge_apply_failure_shows_form_error_and_allows_retry(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An engine failure while applying keeps the flow (and the cached preview)
    alive with a purge_failed error — and the apply is restricted to exactly
    the previewed ids."""
    entry = await _setup_entry(hass)
    preview = {
        "dry_run": True,
        "repack": True,
        "orphans": [
            {
                "entity_id": "sensor.pf_gone",
                "applied": False,
                "deleted_states": 1,
                "deleted_statistics": 0,
            }
        ],
    }
    with patch(
        "custom_components.history_mover.config_flow.async_perform_purge",
        side_effect=[preview, HomeAssistantError("recorder timeout"), preview],
    ) as perform:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "purge"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"repack": True}
        )
        assert result["step_id"] == "purge_confirm"  # call 1: the preview

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )  # call 2: apply fails
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "purge_confirm"
        assert result["errors"] == {"base": "purge_failed"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )  # call 3: retry succeeds
        assert result["type"] is FlowResultType.CREATE_ENTRY
    # Exactly three engine calls: the preview was cached, not re-queried.
    assert perform.call_count == 3
    # Every apply carried the repack choice and the previewed ids.
    for call in perform.call_args_list[1:]:
        assert call.kwargs["dry_run"] is False
        assert call.kwargs["repack"] is True
        assert call.kwargs["restrict_to"] == {"sensor.pf_gone"}


def _purge_item(index: int) -> dict[str, object]:
    return {
        "entity_id": f"sensor.orph_{index}",
        "applied": False,
        "deleted_states": 3,
        "deleted_statistics": 2,
    }


def test_format_purge_preview_caps_large_lists_and_adds_totals() -> None:
    """Hundreds of orphans get totals plus the first ids, not an unreadable
    wall — and the repack choice is spelled out."""
    preview = {
        "dry_run": True,
        "repack": True,
        "orphans": [_purge_item(i) for i in range(20)],
    }
    text = _format_purge_preview(preview)
    assert "**20 orphaned histories**" in text
    assert "delete 60 states / 40 statistics rows" in text
    assert "… and 5 more" in text
    assert "sensor.orph_14" in text  # the 15th listed id
    assert "sensor.orph_15" not in text  # capped after that
    assert "repacked" in text


def test_format_purge_preview_single_orphan_without_repack() -> None:
    text = _format_purge_preview(
        {"dry_run": True, "repack": False, "orphans": [_purge_item(0)]}
    )
    assert "**1 orphaned history**" in text
    assert "`sensor.orph_0`: 3 states / 2 statistics" in text
    assert "repacked" not in text


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
