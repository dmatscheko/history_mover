"""Tests for the history_mover.rename admin service and its validation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.history_mover.const import (
    DOMAIN,
    SERVICE_DELETE,
    SERVICE_PURGE_ORPHANS,
    SERVICE_RENAME,
    SERVICE_REPACK,
)

from .common import (
    add_statistics,
    count_states,
    count_statistics,
    record_states,
    remove_entity,
)


async def _setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SERVICE_RENAME)
    assert hass.services.has_service(DOMAIN, SERVICE_DELETE)
    assert hass.services.has_service(DOMAIN, SERVICE_PURGE_ORPHANS)
    assert hass.services.has_service(DOMAIN, SERVICE_REPACK)


async def _call(hass: HomeAssistant, **data: object) -> dict:
    return await hass.services.async_call(
        DOMAIN, SERVICE_RENAME, data, blocking=True, return_response=True
    )


async def test_single_rename_moves_history(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _setup(hass)
    await record_states(hass, "sensor.old", ["1", "2", "3"])
    await record_states(hass, "sensor.new", ["9"])

    response = await _call(
        hass, old_entity_id="sensor.old", new_entity_id="sensor.new"
    )
    assert response["dry_run"] is False
    assert response["renames"][0]["status"] == "replaced"
    assert response["renames"][0]["moved_states"] == 3
    assert await count_states(hass, "sensor.new") == 3
    assert await count_states(hass, "sensor.old") is None


async def test_bulk_rename_via_list(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _setup(hass)
    await record_states(hass, "sensor.a_old", ["1", "2"])
    await record_states(hass, "sensor.b_old", ["1", "2", "3"])

    response = await _call(
        hass,
        renames=[
            {"old_entity_id": "sensor.a_old", "new_entity_id": "sensor.a_new"},
            {"old_entity_id": "sensor.b_old", "new_entity_id": "sensor.b_new"},
        ],
    )
    assert [r["status"] for r in response["renames"]] == ["renamed", "renamed"]
    assert await count_states(hass, "sensor.a_new") == 2
    assert await count_states(hass, "sensor.b_new") == 3


async def test_dry_run_previews_without_change(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _setup(hass)
    await record_states(hass, "sensor.old", ["1", "2", "3"])

    response = await _call(
        hass,
        old_entity_id="sensor.old",
        new_entity_id="sensor.new",
        dry_run=True,
    )
    assert response["dry_run"] is True
    assert response["renames"][0]["applied"] is False
    assert response["renames"][0]["moved_states"] == 3
    assert await count_states(hass, "sensor.old") == 3  # untouched


@pytest.mark.parametrize(
    "data",
    [
        {},  # neither single pair nor list
        {"old_entity_id": "sensor.old"},  # only one half of the pair
        {"new_entity_id": "sensor.new"},
        {  # same source and target
            "old_entity_id": "sensor.same",
            "new_entity_id": "sensor.same",
        },
        {  # duplicate target in one call
            "renames": [
                {"old_entity_id": "sensor.a", "new_entity_id": "sensor.z"},
                {"old_entity_id": "sensor.b", "new_entity_id": "sensor.z"},
            ]
        },
        {  # duplicate source in one call
            "renames": [
                {"old_entity_id": "sensor.a", "new_entity_id": "sensor.x"},
                {"old_entity_id": "sensor.a", "new_entity_id": "sensor.y"},
            ]
        },
        {  # swap: each id is both a source and a target
            "renames": [
                {"old_entity_id": "sensor.a", "new_entity_id": "sensor.b"},
                {"old_entity_id": "sensor.b", "new_entity_id": "sensor.a"},
            ]
        },
        {  # chain: sensor.b is a target and a source
            "renames": [
                {"old_entity_id": "sensor.a", "new_entity_id": "sensor.b"},
                {"old_entity_id": "sensor.b", "new_entity_id": "sensor.c"},
            ]
        },
    ],
)
async def test_validation_errors(
    recorder_mock: Recorder, hass: HomeAssistant, data: dict
) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, **data)


async def test_reference_scan_reports_and_notifies(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _setup(hass)
    await record_states(hass, "sensor.old", ["1", "2"])
    # A lingering reference to the source id in a config file.
    from pathlib import Path

    automations = Path(hass.config.config_dir) / "automations.yaml"
    automations.write_text(
        "- alias: t\n  triggers:\n    - trigger: state\n      entity_id: sensor.old\n",
        encoding="utf-8",
    )

    with patch(
        "custom_components.history_mover.services.persistent_notification.async_create"
    ) as notify:
        response = await _call(
            hass, old_entity_id="sensor.old", new_entity_id="sensor.new"
        )

    assert "sensor.old" in response["references"]
    assert response["references"]["sensor.old"][0]["source"] == "automations.yaml"
    assert notify.called


async def test_delete_service_end_to_end(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Preview with dry_run (typos reported), then apply — via the service."""
    await _setup(hass)
    await record_states(hass, "sensor.svcdel_gone", ["1"])
    await remove_entity(hass, "sensor.svcdel_gone")
    await record_states(hass, "sensor.svcdel_keep", ["1"])

    preview = await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE,
        {"entity_ids": ["sensor.svcdel_gone", "sensor.svcdel_typo"], "dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert preview["deletions"] == [
        {
            "entity_id": "sensor.svcdel_gone",
            "applied": False,
            "deleted_states": 2,
            "deleted_statistics": 0,
        }
    ]
    assert preview["not_found_entity_ids"] == ["sensor.svcdel_typo"]
    assert preview["not_found_domains"] == []
    assert await count_states(hass, "sensor.svcdel_gone") == 2  # untouched

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE,
        {"entity_ids": ["sensor.svcdel_gone"]},
        blocking=True,
        return_response=True,
    )
    assert response["deletions"][0]["applied"] is True
    assert await count_states(hass, "sensor.svcdel_gone") is None
    assert await count_states(hass, "sensor.svcdel_keep") == 1


async def test_delete_service_rejects_empty_selection(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError, match="at least one"):
        await hass.services.async_call(
            DOMAIN, SERVICE_DELETE, {}, blocking=True, return_response=True
        )


async def test_purge_orphans_service_end_to_end(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Preview with dry_run, then apply — both through the registered service."""
    await _setup(hass)
    await record_states(hass, "sensor.svc_alive", ["1"])
    await record_states(hass, "sensor.svc_gone", ["1", "2"])
    await remove_entity(hass, "sensor.svc_gone")
    await add_statistics(hass, "sensor.svc_gone", [1.0])

    preview = await hass.services.async_call(
        DOMAIN,
        SERVICE_PURGE_ORPHANS,
        {"dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert preview["dry_run"] is True
    assert preview["repack"] is False
    assert preview["orphans"] == [
        {
            "entity_id": "sensor.svc_gone",
            "applied": False,
            "deleted_states": 3,
            "deleted_statistics": 1,
        }
    ]
    assert await count_states(hass, "sensor.svc_gone") == 3  # untouched

    response = await hass.services.async_call(
        DOMAIN, SERVICE_PURGE_ORPHANS, {}, blocking=True, return_response=True
    )
    assert response["orphans"][0]["applied"] is True
    assert await count_states(hass, "sensor.svc_gone") is None
    assert await count_statistics(hass, "sensor.svc_gone") is None
    assert await count_states(hass, "sensor.svc_alive") == 1


async def test_repack_service_runs_repack(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The field-less repack service runs core's repack and blocks until done."""
    await _setup(hass)
    with patch(
        "custom_components.history_mover.purger.repack_database"
    ) as repack:
        await hass.services.async_call(DOMAIN, SERVICE_REPACK, {}, blocking=True)
    repack.assert_called_once()


async def test_reference_scan_can_be_disabled(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await _setup(hass)
    await record_states(hass, "sensor.old", ["1", "2"])

    response = await _call(
        hass,
        old_entity_id="sensor.old",
        new_entity_id="sensor.new",
        scan_references=False,
    )
    assert "references" not in response
