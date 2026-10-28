"""Tests for diagnostics."""

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from custom_components.gitea_todo.const import CONF_API_TOKEN

from .conftest import SUBENTRY_ID
from .fake_gitea import Failure, FakeGitea


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    fake_gitea: FakeGitea,
    snapshot: SnapshotAssertion,
) -> None:
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )
    assert diagnostics["entry"]["data"][CONF_API_TOKEN] == "**REDACTED**"
    # The fake server's port changes between runs.
    diagnostics["entry"]["data"]["url"] = "http://127.0.0.1:PORT"
    assert diagnostics == snapshot


async def test_diagnostics_failed_repository(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    config_entry: MockConfigEntry,
    fake_gitea: FakeGitea,
) -> None:
    fake_gitea.failures.append(Failure("/repositories/7", 500))
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )
    repository = diagnostics["repositories"][SUBENTRY_ID]
    assert repository["last_update_success"] is False
    assert repository["repository"] is None
    assert repository["open_issues"] is None
    assert "UpdateFailed" in repository["last_exception"]
