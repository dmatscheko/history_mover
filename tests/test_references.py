"""Tests for the report-only reference scan.

The test harness shares one config directory across the session, so each test
uses entity ids unique to itself — that way files another test left behind can
never produce a false match here.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.history_mover.references import async_scan_references


async def test_scans_root_yaml_and_storage(hass: HomeAssistant) -> None:
    config_dir = Path(hass.config.config_dir)
    (config_dir / "automations.yaml").write_text(
        "entity_id: sensor.refscan_power\nentity_id: sensor.refscan_power\n",
        encoding="utf-8",
    )
    storage = config_dir / ".storage"
    storage.mkdir(exist_ok=True)
    (storage / "lovelace.refscan_dash").write_text(
        '{"entity": "sensor.refscan_power"}', encoding="utf-8"
    )

    refs = await async_scan_references(hass, ["sensor.refscan_power"])
    by_source = {hit.source: hit.count for hit in refs["sensor.refscan_power"]}
    assert by_source["automations.yaml"] == 2
    assert by_source[".storage/lovelace.refscan_dash"] == 1


async def test_whole_id_match_only(hass: HomeAssistant) -> None:
    config_dir = Path(hass.config.config_dir)
    (config_dir / "scripts.yaml").write_text(
        "sensor.refwhole_total and binary_sensor.refwhole, never the bare id\n",
        encoding="utf-8",
    )
    # sensor.refwhole appears only as a substring of longer ids -> no whole match.
    assert await async_scan_references(hass, ["sensor.refwhole"]) == {}


async def test_empty_ids_return_empty(hass: HomeAssistant) -> None:
    assert await async_scan_references(hass, []) == {}
    assert await async_scan_references(hass, [""]) == {}


async def test_absent_id_is_not_found(hass: HomeAssistant) -> None:
    assert await async_scan_references(hass, ["sensor.refabsent_zzz"]) == {}
