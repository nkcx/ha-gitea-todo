"""Fixtures for Gitea To-do tests."""

from collections.abc import AsyncGenerator
from types import MappingProxyType
from typing import Any

from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import (
    HomeAssistantSnapshotExtension,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.gitea_todo.const import (
    CONF_ALLOW_CLOSING,
    CONF_API_TOKEN,
    CONF_COMPLETED_DAYS,
    CONF_EXCLUDE_LABELS,
    CONF_INCLUDE_LABELS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DOMAIN,
    SUBENTRY_TYPE_REPOSITORY,
)

from .fake_gitea import TOKEN, FakeGitea, make_app

SUBENTRY_ID = "01K8GITEAREPOSUBENTRY0000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom_components/gitea_todo."""


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use the Home Assistant snapshot extension (snapshots/ directory)."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
async def fake_gitea(socket_enabled: None) -> AsyncGenerator[FakeGitea]:
    """Run a fake Gitea API on 127.0.0.1."""
    fake = FakeGitea()
    server = TestServer(make_app(fake), host="127.0.0.1")
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options of the test config entry."""
    return {CONF_SCAN_INTERVAL: 15, CONF_COMPLETED_DAYS: 7, CONF_ALLOW_CLOSING: False}


@pytest.fixture
def repository_data() -> dict[str, Any]:
    """Return the data of the test repository subentry."""
    return {
        CONF_REPOSITORY: "infra/metatron",
        CONF_REPOSITORY_ID: 7,
        CONF_INCLUDE_LABELS: [],
        CONF_EXCLUDE_LABELS: ["wontfix"],
    }


@pytest.fixture
def config_entry(
    fake_gitea: FakeGitea,
    entry_options: dict[str, Any],
    repository_data: dict[str, Any],
) -> MockConfigEntry:
    """Return a config entry for the fake server with one repository."""
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id="01K8GITEATESTENTRY0000000",
        title="alice @ 127.0.0.1",
        unique_id=f"{fake_gitea.url}#1",
        data={CONF_URL: fake_gitea.url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
        options=entry_options,
        subentries_data=[
            ConfigSubentryData(
                data=MappingProxyType(repository_data),
                subentry_id=SUBENTRY_ID,
                subentry_type=SUBENTRY_TYPE_REPOSITORY,
                title="infra/metatron",
                unique_id="7",
            )
        ],
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
