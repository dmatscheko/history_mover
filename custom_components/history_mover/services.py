"""The ``history_mover.rename`` admin service.

One call carries one pair (``old_entity_id`` + ``new_entity_id``) or many
(``renames``). It is admin-only and returns response data — a per-pair report
and, for a dry run, a preview — so it is equally usable from Developer Tools,
scripts, and the guided UI (which calls the same engine).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    ATTR_DRY_RUN,
    ATTR_NEW_ENTITY_ID,
    ATTR_OLD_ENTITY_ID,
    ATTR_ON_CONFLICT,
    ATTR_RENAMES,
    ATTR_SCAN_REFERENCES,
    CONFLICT_MODES,
    DEFAULT_ON_CONFLICT,
    DOMAIN,
    SERVICE_RENAME,
    STATUS_RENAMED,
    STATUS_REPLACED,
)
from .mover import RenameRequest, async_move_history
from .references import ReferenceHit, async_scan_references

if TYPE_CHECKING:
    from collections.abc import Mapping

_NOTIFICATION_ID = f"{DOMAIN}_references"

_RENAME_ITEM = vol.Schema(
    {
        vol.Required(ATTR_OLD_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_NEW_ENTITY_ID): cv.entity_id,
    }
)

_RENAME_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_OLD_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_NEW_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_RENAMES): vol.All(cv.ensure_list, [_RENAME_ITEM]),
        vol.Optional(ATTR_ON_CONFLICT, default=DEFAULT_ON_CONFLICT): vol.In(CONFLICT_MODES),
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_SCAN_REFERENCES, default=True): cv.boolean,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the rename service once."""
    if hass.services.has_service(DOMAIN, SERVICE_RENAME):
        return

    async def _handle_rename(call: ServiceCall) -> ServiceResponse:
        return await async_perform_rename(
            hass,
            _collect_requests(call.data),
            on_conflict=call.data[ATTR_ON_CONFLICT],
            dry_run=call.data[ATTR_DRY_RUN],
            scan_references=call.data[ATTR_SCAN_REFERENCES],
        )

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RENAME,
        _handle_rename,
        schema=_RENAME_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_perform_rename(
    hass: HomeAssistant,
    requests: list[RenameRequest],
    *,
    on_conflict: str,
    dry_run: bool,
    scan_references: bool,
) -> dict[str, Any]:
    """Move history and (optionally) report lingering references.

    Shared by the service and the guided UI so both behave identically. Returns
    the response dict the service exposes and the UI reads for its preview.
    """
    outcomes = await async_move_history(
        hass, requests, on_conflict=on_conflict, dry_run=dry_run
    )
    response: dict[str, Any] = {
        "dry_run": dry_run,
        "renames": [outcome.as_dict() for outcome in outcomes],
    }
    if scan_references:
        moved = [
            outcome.old_entity_id
            for outcome in outcomes
            if outcome.status in (STATUS_RENAMED, STATUS_REPLACED)
        ]
        references = await async_scan_references(hass, moved)
        response["references"] = {
            entity_id: [hit.as_dict() for hit in hits]
            for entity_id, hits in references.items()
        }
        if references and not dry_run:
            _notify_lingering_references(hass, references)
    return response


def _collect_requests(data: Mapping[str, Any]) -> list[RenameRequest]:
    """Normalise the single-pair and list forms into one validated list."""
    pairs: list[RenameRequest] = []
    if ATTR_RENAMES in data:
        pairs.extend(
            RenameRequest(item[ATTR_OLD_ENTITY_ID], item[ATTR_NEW_ENTITY_ID])
            for item in data[ATTR_RENAMES]
        )
    has_old = ATTR_OLD_ENTITY_ID in data
    has_new = ATTR_NEW_ENTITY_ID in data
    if has_old != has_new:
        raise ServiceValidationError(
            "Provide both old_entity_id and new_entity_id, or neither."
        )
    if has_old and has_new:
        pairs.append(RenameRequest(data[ATTR_OLD_ENTITY_ID], data[ATTR_NEW_ENTITY_ID]))
    if not pairs:
        raise ServiceValidationError(
            "Provide old_entity_id and new_entity_id, or a renames list."
        )
    _reject_ambiguous(pairs)
    return pairs


def _reject_ambiguous(pairs: list[RenameRequest]) -> None:
    """A pair must move a distinct source to a distinct target within one call."""
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    for pair in pairs:
        if pair.old_entity_id == pair.new_entity_id:
            raise ServiceValidationError(
                f"Source and target are the same id: {pair.old_entity_id}"
            )
        if pair.old_entity_id in seen_old:
            raise ServiceValidationError(
                f"The same source appears twice in one call: {pair.old_entity_id}"
            )
        if pair.new_entity_id in seen_new:
            raise ServiceValidationError(
                f"The same target appears twice in one call: {pair.new_entity_id}"
            )
        seen_old.add(pair.old_entity_id)
        seen_new.add(pair.new_entity_id)


@callback
def _notify_lingering_references(
    hass: HomeAssistant, references: dict[str, list[ReferenceHit]]
) -> None:
    """Surface a report of where the old ids still appear (never auto-edited)."""
    lines = [
        f"- `{entity_id}` still referenced in: "
        + ", ".join(f"{hit.source} ({hit.count} matches)" for hit in hits)
        for entity_id, hits in references.items()
    ]
    message = (
        "History was moved, but the old entity id is still used in your "
        "configuration. History Mover never edits these — update them to the "
        "new id yourself:\n\n" + "\n".join(lines)
    )
    persistent_notification.async_create(
        hass,
        message,
        title="History Mover: references to update",
        notification_id=_NOTIFICATION_ID,
    )
