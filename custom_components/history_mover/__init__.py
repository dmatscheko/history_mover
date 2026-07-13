"""History Mover: move recorder history (states + statistics) between entities.

A tool, not a device: it registers one admin service and a singleton config
entry whose only job is to give the guided rename UI a home in Settings. There
are no entities and nothing to poll.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .services import async_setup_services

# Config-entry only; nothing is configured from configuration.yaml.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the rename service as soon as the integration is loaded."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the singleton entry (the service is already registered)."""
    async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entry. The service stays registered for the process lifetime."""
    return True
