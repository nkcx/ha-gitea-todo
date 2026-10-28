"""Diagnostics for the Gitea To-do integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN
from .coordinator import GiteaConfigEntry

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GiteaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (the token is redacted)."""
    runtime = entry.runtime_data
    repositories: dict[str, Any] = {}
    for subentry_id, coordinator in runtime.repositories.items():
        data = coordinator.data
        repositories[subentry_id] = {
            "subentry": dict(entry.subentries[subentry_id].data),
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "repository": None
            if data is None
            else {
                "id": data.repository.id,
                "full_name": data.repository.full_name,
                "has_issues": data.repository.has_issues,
                "can_write": data.repository.can_write,
            },
            "open_issues": None if data is None else len(data.open_issues),
            "closed_issues": None if data is None else len(data.closed_issues),
        }
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "server": {"version": runtime.version, "user": runtime.user.login},
        "repositories": repositories,
    }
