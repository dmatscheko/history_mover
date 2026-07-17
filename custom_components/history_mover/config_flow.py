"""Config flow (add the tool) and options flow (the guided wizards).

The config entry carries no data — it exists only so the tool has a card in
Settings whose **Configure** button opens the options flow. That flow is the
UI twin of the two services: pick a single pair or a bulk prefix swap for a
``rename``, or the orphan purge, see a dry-run preview of exactly what moves
or gets deleted, then apply.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    ATTR_DOMAINS,
    ATTR_ENTITY_IDS,
    ATTR_NEW_ENTITY_ID,
    ATTR_NEW_PREFIX,
    ATTR_OLD_ENTITY_ID,
    ATTR_OLD_PREFIX,
    ATTR_ON_CONFLICT,
    ATTR_REPACK,
    CONFLICT_MODES,
    DEFAULT_ON_CONFLICT,
    DOMAIN,
)
from .mover import RenameRequest, async_list_history_ids
from .purger import valid_delete_domain
from .services import (
    async_perform_delete,
    async_perform_purge,
    async_perform_rename,
)

_LOGGER = logging.getLogger(__name__)


def _conflict_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=list(CONFLICT_MODES),
            translation_key="on_conflict",
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _validated_entity_id(value: str, field: str, errors: dict[str, str]) -> str:
    """Normalise with the service's own validator (strip, lowercase, validate).

    On failure, records a per-field ``invalid_entity_id`` error and returns "".
    """
    try:
        return cv.entity_id(value.strip())
    except vol.Invalid:
        errors[field] = "invalid_entity_id"
        return ""


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
    """Pick a rename (single or bulk), a targeted delete, or the orphan purge;
    preview, confirm, apply."""

    def __init__(self) -> None:
        self._pairs: list[RenameRequest] = []
        self._on_conflict: str = DEFAULT_ON_CONFLICT
        self._summary: str | None = None
        self._repack: bool = False
        self._purge_ids: set[str] = set()
        self._delete_selection: tuple[list[str], list[str]] = ([], [])
        self._delete_ids: set[str] = set()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["single", "bulk", "delete", "purge"]
        )

    async def async_step_single(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            old = _validated_entity_id(
                user_input[ATTR_OLD_ENTITY_ID], ATTR_OLD_ENTITY_ID, errors
            )
            new = _validated_entity_id(
                user_input[ATTR_NEW_ENTITY_ID], ATTR_NEW_ENTITY_ID, errors
            )
            if not errors and old == new:
                errors["base"] = "same_id"
            if not errors:
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
            # Recorder ids are always lower-case, so normalise like the ids are.
            old_prefix = user_input[ATTR_OLD_PREFIX].strip().lower()
            new_prefix = user_input[ATTR_NEW_PREFIX].strip().lower()
            if not old_prefix:
                # An empty source prefix would match every id in the recorder.
                errors[ATTR_OLD_PREFIX] = "empty_prefix"
            else:
                ids = await async_list_history_ids(self.hass, old_prefix)
                pairs = [
                    RenameRequest(
                        history_id, new_prefix + history_id[len(old_prefix) :]
                    )
                    for history_id in ids
                ]
                # Drop no-ops (e.g. an unchanged prefix maps an id to itself).
                pairs = [p for p in pairs if p.old_entity_id != p.new_entity_id]
                if not pairs:
                    errors["base"] = "no_matches"
                elif not all(valid_entity_id(p.new_entity_id) for p in pairs):
                    errors[ATTR_NEW_PREFIX] = "invalid_target"
                elif {p.new_entity_id for p in pairs} & set(ids):
                    # A generated target is itself one of the matched sources
                    # (the target prefix extends the source prefix) — the engine
                    # rejects such batches as order-dependent.
                    errors[ATTR_NEW_PREFIX] = "overlapping"
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
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await async_perform_rename(
                    self.hass,
                    self._pairs,
                    on_conflict=self._on_conflict,
                    dry_run=False,
                    scan_references=True,
                )
            except HomeAssistantError:
                # Keep the flow (and the user's input) alive with a real error
                # instead of the generic unknown-error toast.
                _LOGGER.exception("Applying the move from the guided flow failed")
                errors["base"] = "apply_failed"
            else:
                return self.async_create_entry(title="", data={})
        if self._summary is None:
            # Computed once; on an apply error the cached preview is re-shown
            # rather than re-queried from a recorder that just failed.
            preview = await async_perform_rename(
                self.hass,
                self._pairs,
                on_conflict=self._on_conflict,
                dry_run=True,
                scan_references=False,
            )
            self._summary = _format_preview(preview)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"summary": self._summary},
        )

    async def async_step_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick entity ids and/or domains to delete, then preview (a dry run)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_ids = [v for v in user_input[ATTR_ENTITY_IDS] if v.strip()]
            domains = [v for v in user_input[ATTR_DOMAINS] if v.strip()]
            if not entity_ids and not domains:
                errors["base"] = "empty_selection"
            elif not all(valid_delete_domain(value) for value in domains):
                errors[ATTR_DOMAINS] = "invalid_domain"
            else:
                preview = await async_perform_delete(
                    self.hass,
                    entity_ids=entity_ids,
                    domains=domains,
                    dry_run=True,
                    repack=user_input[ATTR_REPACK],
                )
                if not preview["deletions"]:
                    errors["base"] = "no_delete_matches"
                else:
                    self._delete_selection = (entity_ids, domains)
                    self._repack = user_input[ATTR_REPACK]
                    self._delete_ids = {
                        item["entity_id"] for item in preview["deletions"]
                    }
                    self._summary = _format_delete_preview(preview)
                    return await self.async_step_delete_confirm()
        return self.async_show_form(
            step_id="delete",
            data_schema=vol.Schema(
                {
                    vol.Optional(ATTR_ENTITY_IDS, default=[]): TextSelector(
                        TextSelectorConfig(multiple=True)
                    ),
                    vol.Optional(ATTR_DOMAINS, default=[]): TextSelector(
                        TextSelectorConfig(multiple=True)
                    ),
                    vol.Required(ATTR_REPACK, default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_delete_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the delete preview, then apply on submit.

        The apply re-matches the same selection but is restricted to the
        previewed ids — nothing that appeared since the preview is deleted.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_ids, domains = self._delete_selection
            try:
                await async_perform_delete(
                    self.hass,
                    entity_ids=entity_ids,
                    domains=domains,
                    dry_run=False,
                    repack=self._repack,
                    restrict_to=self._delete_ids,
                )
            except HomeAssistantError:
                # Keep the flow (and the cached preview) alive with a real
                # error instead of the generic unknown-error toast.
                _LOGGER.exception("Applying the delete from the guided flow failed")
                errors["base"] = "delete_failed"
            else:
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="delete_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            # Always set by async_step_delete before this step is reachable.
            description_placeholders={"summary": self._summary or ""},
        )

    async def async_step_purge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether to repack, then look for orphans (a dry run)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            preview = await async_perform_purge(
                self.hass, dry_run=True, repack=user_input[ATTR_REPACK]
            )
            if not preview["orphans"]:
                errors["base"] = "no_orphans"
            else:
                self._repack = user_input[ATTR_REPACK]
                self._purge_ids = {
                    item["entity_id"] for item in preview["orphans"]
                }
                self._summary = _format_purge_preview(preview)
                return await self.async_step_purge_confirm()
        return self.async_show_form(
            step_id="purge",
            data_schema=vol.Schema(
                {vol.Required(ATTR_REPACK, default=False): BooleanSelector()}
            ),
            errors=errors,
        )

    async def async_step_purge_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the orphan preview, then apply on submit.

        The apply is restricted to the previewed ids *and* re-checks each is
        still orphaned — nothing unseen is deleted, nothing revived is lost.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await async_perform_purge(
                    self.hass,
                    dry_run=False,
                    repack=self._repack,
                    restrict_to=self._purge_ids,
                )
            except HomeAssistantError:
                # Keep the flow (and the cached preview) alive with a real
                # error instead of the generic unknown-error toast.
                _LOGGER.exception("Applying the purge from the guided flow failed")
                errors["base"] = "purge_failed"
            else:
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="purge_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            # Always set by async_step_purge before this step is reachable.
            description_placeholders={"summary": self._summary or ""},
        )


# The confirm dialog lists at most this many pairs; a bulk move of hundreds
# gets totals plus a "… and N more" line instead of an unreadable wall.
_PREVIEW_MAX_PAIRS = 15


def _format_preview(preview: dict[str, Any]) -> str:
    """A markdown summary for the confirm screen: totals for multi-pair
    batches, then one line per pair, capped at ``_PREVIEW_MAX_PAIRS``."""
    renames: list[dict[str, Any]] = preview["renames"]
    lines = [
        f"- `{item['old_entity_id']}` → `{item['new_entity_id']}`: "
        f"**{item['status']}** (move {item['moved_states']} states / "
        f"{item['moved_statistics']} statistics; discard {item['discarded_states']} / "
        f"{item['discarded_statistics']})"
        for item in renames[:_PREVIEW_MAX_PAIRS]
    ]
    if len(renames) > _PREVIEW_MAX_PAIRS:
        lines.append(f"- … and {len(renames) - _PREVIEW_MAX_PAIRS} more pairs")
    if len(renames) > 1:
        statuses = ", ".join(
            f"{count} {status}"
            for status, count in Counter(
                item["status"] for item in renames
            ).most_common()
        )
        lines.insert(
            0,
            f"**{len(renames)} pairs** ({statuses}) — move "
            f"{sum(i['moved_states'] for i in renames)} states / "
            f"{sum(i['moved_statistics'] for i in renames)} statistics; discard "
            f"{sum(i['discarded_states'] for i in renames)} / "
            f"{sum(i['discarded_statistics'] for i in renames)}\n",
        )
    return "\n".join(lines)


_REPACK_NOTE = (
    "\nThe database is repacked afterwards to reclaim the freed disk"
    " space — this can take a while on a large database."
)


def _deletion_lines(
    items: list[dict[str, Any]], noun_one: str, noun_many: str
) -> list[str]:
    """Totals, then one line per id, capped at ``_PREVIEW_MAX_PAIRS`` — the
    shared body of the delete and purge confirm summaries."""
    lines = [
        f"**{len(items)} {noun_many if len(items) > 1 else noun_one}** — delete "
        f"{sum(i['deleted_states'] for i in items)} states / "
        f"{sum(i['deleted_statistics'] for i in items)} statistics rows\n"
    ]
    lines.extend(
        f"- `{item['entity_id']}`: {item['deleted_states']} states / "
        f"{item['deleted_statistics']} statistics"
        for item in items[:_PREVIEW_MAX_PAIRS]
    )
    if len(items) > _PREVIEW_MAX_PAIRS:
        lines.append(f"- … and {len(items) - _PREVIEW_MAX_PAIRS} more")
    return lines


def _format_purge_preview(preview: dict[str, Any]) -> str:
    """A markdown summary for the purge confirm screen: totals, then one line
    per orphan, plus the repack choice."""
    lines = _deletion_lines(preview["orphans"], "orphaned history", "orphaned histories")
    if preview["repack"]:
        lines.append(_REPACK_NOTE)
    return "\n".join(lines)


def _format_delete_preview(preview: dict[str, Any]) -> str:
    """A markdown summary for the delete confirm screen: totals and per-id
    lines, warnings for selection parts that matched nothing, the repack choice."""
    lines = _deletion_lines(preview["deletions"], "history", "histories")
    not_found = [
        ("No recorder history found for", preview["not_found_entity_ids"]),
        ("No recorder history found in domain", preview["not_found_domains"]),
    ]
    if any(missing for _, missing in not_found):
        lines.append("")
        lines.extend(
            f"{label}: " + ", ".join(f"`{name}`" for name in missing)
            for label, missing in not_found
            if missing
        )
    if preview["repack"]:
        lines.append(_REPACK_NOTE)
    return "\n".join(lines)
