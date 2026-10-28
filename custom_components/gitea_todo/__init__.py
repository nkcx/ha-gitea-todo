"""The Gitea To-do integration: Gitea issues as Home Assistant to-do lists."""

from __future__ import annotations

import asyncio

from homeassistant.const import CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import GiteaAuthError, GiteaClient, GiteaError
from .const import CONF_API_TOKEN, DOMAIN, SUBENTRY_TYPE_REPOSITORY
from .coordinator import (
    GiteaConfigEntry,
    GiteaRepositoryCoordinator,
    GiteaRuntimeData,
    repair_issue_id,
)

PLATFORMS = [Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Gitea To-do integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GiteaConfigEntry) -> bool:
    """Connect to Gitea and start tracking the configured repositories."""
    client = GiteaClient(
        entry.data[CONF_URL],
        entry.data[CONF_API_TOKEN],
        async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL]),
    )
    try:
        user = await client.async_get_user()
        version = await client.async_get_version()
    except GiteaAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except GiteaError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    runtime = GiteaRuntimeData(client=client, user=user, version=version)
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_REPOSITORY):
        runtime.repositories[subentry.subentry_id] = GiteaRepositoryCoordinator(
            hass, entry, client, subentry
        )
    # A missing or unreadable repository shouldn't keep the others from loading,
    # so failures make that repository's list unavailable instead of failing setup.
    await asyncio.gather(
        *(coordinator.async_refresh() for coordinator in runtime.repositories.values())
    )
    _remove_stale_repairs(hass, entry, runtime)
    entry.runtime_data = runtime

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: GiteaConfigEntry) -> None:
    """Reload after options or repositories change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GiteaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: GiteaConfigEntry) -> None:
    """Remove the entry's repair issues."""
    for subentry_id in entry.subentries:
        ir.async_delete_issue(hass, DOMAIN, repair_issue_id(subentry_id))


def _remove_stale_repairs(
    hass: HomeAssistant, entry: GiteaConfigEntry, runtime: GiteaRuntimeData
) -> None:
    """Delete repair issues of repositories that were removed from the entry."""
    prefix = repair_issue_id("")
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain != DOMAIN or not issue_id.startswith(prefix):
            continue
        subentry_id = issue_id.removeprefix(prefix)
        known = any(subentry_id in other.subentries for other in _entries(hass))
        if not known and subentry_id not in runtime.repositories:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _entries(hass: HomeAssistant) -> list[GiteaConfigEntry]:
    return hass.config_entries.async_entries(DOMAIN)
