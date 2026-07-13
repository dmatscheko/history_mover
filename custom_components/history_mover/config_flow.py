"""Config flow (add the tool) and options flow (the guided rename wizard).

The config entry carries no data — it exists only so the tool has a card in
Settings whose **Configure** button opens the options flow. That flow is the
UI twin of the ``rename`` service: pick a single pair or a bulk prefix swap,
see a dry-run preview of exactly what moves and what gets discarded, then apply.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    ATTR_NEW_ENTITY_ID,
    ATTR_OLD_ENTITY_ID,
    ATTR_ON_CONFLICT,
    CONFLICT_MODES,
    DEFAULT_ON_CONFLICT,
    DOMAIN,
)
from .mover import RenameRequest, async_list_history_ids
from .services import async_perform_rename

ATTR_OLD_PREFIX = "old_prefix"
ATTR_NEW_PREFIX = "new_prefix"


def _conflict_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=list(CONFLICT_MODES),
            translation_key="on_conflict",
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class HistoryMoverConfigFlow(ConfigFlow, domain=DOMAIN):
    """One singleton entry. It carries no data — it only hosts the options flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add the integration. ``single_config_entry`` in the manifest makes
        Home Assistant abort a second attempt before this step is reached."""
        if user_input is not None:
            return self.async_create_entry(title="History Mover", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> HistoryMoverOptionsFlow:
        return HistoryMoverOptionsFlow()


class HistoryMoverOptionsFlow(OptionsFlow):
    """Pick single or bulk, preview the move, confirm, apply."""

    def __init__(self) -> None:
        self._pairs: list[RenameRequest] = []
        self._on_conflict: str = DEFAULT_ON_CONFLICT

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["single", "bulk"])

    async def async_step_single(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            old = user_input[ATTR_OLD_ENTITY_ID].strip()
            new = user_input[ATTR_NEW_ENTITY_ID].strip()
            if old == new:
                errors["base"] = "same_id"
            else:
                self._pairs = [RenameRequest(old, new)]
                self._on_conflict = user_input[ATTR_ON_CONFLICT]
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="single",
            data_schema=vol.Schema(
                {
                    vol.Required(ATTR_OLD_ENTITY_ID): TextSelector(),
                    vol.Required(ATTR_NEW_ENTITY_ID): TextSelector(),
                    vol.Required(
                        ATTR_ON_CONFLICT, default=DEFAULT_ON_CONFLICT
                    ): _conflict_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_bulk(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            old_prefix = user_input[ATTR_OLD_PREFIX].strip()
            new_prefix = user_input[ATTR_NEW_PREFIX].strip()
            ids = await async_list_history_ids(self.hass, old_prefix)
            pairs = [
                RenameRequest(history_id, new_prefix + history_id[len(old_prefix) :])
                for history_id in ids
            ]
            # Drop no-ops (e.g. an unchanged prefix maps an id to itself).
            pairs = [p for p in pairs if p.old_entity_id != p.new_entity_id]
            if not pairs:
                errors["base"] = "no_matches"
            else:
                self._pairs = pairs
                self._on_conflict = user_input[ATTR_ON_CONFLICT]
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="bulk",
            data_schema=vol.Schema(
                {
                    vol.Required(ATTR_OLD_PREFIX): TextSelector(),
                    vol.Required(ATTR_NEW_PREFIX): TextSelector(),
                    vol.Required(
                        ATTR_ON_CONFLICT, default=DEFAULT_ON_CONFLICT
                    ): _conflict_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a dry-run preview, then apply on submit."""
        if user_input is not None:
            await async_perform_rename(
                self.hass,
                self._pairs,
                on_conflict=self._on_conflict,
                dry_run=False,
                scan_references=True,
            )
            return self.async_create_entry(title="", data={})
        preview = await async_perform_rename(
            self.hass,
            self._pairs,
            on_conflict=self._on_conflict,
            dry_run=True,
            scan_references=False,
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": _format_preview(preview)},
        )


def _format_preview(preview: dict[str, Any]) -> str:
    """A one-line-per-pair markdown summary for the confirm screen."""
    return "\n".join(
        f"- `{item['old_entity_id']}` → `{item['new_entity_id']}`: "
        f"**{item['status']}** (move {item['moved_states']} states / "
        f"{item['moved_statistics']} statistics; discard {item['discarded_states']} / "
        f"{item['discarded_statistics']})"
        for item in preview["renames"]
    )
