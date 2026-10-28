"""Tests for setting up and unloading the integration."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigSubentry,
    ConfigSubentryData,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.gitea_todo.const import (
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DOMAIN,
    SUBENTRY_TYPE_REPOSITORY,
)
from custom_components.gitea_todo.coordinator import repair_issue_id

from .conftest import SUBENTRY_ID
from .fake_gitea import Failure, FakeGitea

ENTITY_ID = "todo.infra_metatron_issues"


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY_ID) is not None
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED


async def test_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    fake_gitea: FakeGitea,
) -> None:
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{fake_gitea.url}#1_7"), init_integration.entry_id
    )
    assert device is not None
    assert device.name == "infra/metatron"
    assert device.manufacturer == "Gitea"
    assert device.model == "Repository"
    assert device.sw_version == "1.24.6"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.configuration_url == f"{fake_gitea.url}/infra/metatron"
    assert device.config_entries_subentries == {
        init_integration.entry_id: {SUBENTRY_ID}
    }


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    fake_gitea.failures.append(Failure("/user", 401))
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_setup_connection_failure_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    fake_gitea.failures.append(Failure("/version", 502))
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_repository_not_found_raises_repair(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_gitea: FakeGitea,
    issue_registry: ir.IssueRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    removed = fake_gitea.repos.pop(7)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # The other repositories (none here) still load; this list is unavailable.
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    issue = issue_registry.async_get_issue(DOMAIN, repair_issue_id(SUBENTRY_ID))
    assert issue is not None
    assert issue.translation_placeholders == {
        "repository": "infra/metatron",
        "entry": "alice @ 127.0.0.1",
    }

    fake_gitea.repos[7] = removed
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY_ID).state == "3"
    assert issue_registry.async_get_issue(DOMAIN, repair_issue_id(SUBENTRY_ID)) is None


async def test_auth_failure_during_update_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_gitea: FakeGitea,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_gitea.failures.append(Failure("/repositories", 401))
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_options_change_reloads(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    hass.config_entries.async_update_entry(
        init_integration, options={**init_integration.options, "completed_days": 0}
    )
    await hass.async_block_till_done()
    # Closed issues are no longer fetched or shown.
    assert init_integration.state is ConfigEntryState.LOADED
    coordinator = init_integration.runtime_data.repositories[SUBENTRY_ID]
    assert coordinator.data.closed_issues == []


async def test_add_and_remove_repository(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    fake_gitea: FakeGitea,
) -> None:
    subentry = ConfigSubentryData(
        data={CONF_REPOSITORY: "alice/notes", CONF_REPOSITORY_ID: 8},
        subentry_type=SUBENTRY_TYPE_REPOSITORY,
        title="alice/notes",
        unique_id="8",
    )
    hass.config_entries.async_add_subentry(init_integration, ConfigSubentry(**subentry))
    await hass.async_block_till_done()
    assert hass.states.get("todo.alice_notes_issues").state == "0"

    hass.config_entries.async_remove_subentry(init_integration, SUBENTRY_ID)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID) is None
    assert entity_registry.async_get(ENTITY_ID) is None
    assert not device_registry.async_get_devices(
        identifiers={(DOMAIN, f"{fake_gitea.url}#1_7")}
    )


async def test_stale_repair_removed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        repair_issue_id("gone"),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="repository_not_found",
    )
    ir.async_create_issue(
        hass,
        "other_domain",
        repair_issue_id("gone"),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="x",
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, repair_issue_id("gone")) is None
    assert issue_registry.async_get_issue("other_domain", repair_issue_id("gone"))


async def test_remove_entry_deletes_repairs(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    fake_gitea: FakeGitea,
    issue_registry: ir.IssueRegistry,
) -> None:
    fake_gitea.repos.pop(7)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, repair_issue_id(SUBENTRY_ID))
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, repair_issue_id(SUBENTRY_ID)) is None
