"""Tests for the config, options and repository subentry flows."""

from typing import Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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

from .conftest import SUBENTRY_ID
from .fake_gitea import OTHER_TOKEN, TOKEN, Failure, FakeGitea


async def _user_flow(
    hass: HomeAssistant, url: str, token: str = TOKEN
) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: url, CONF_API_TOKEN: token, CONF_VERIFY_SSL: True},
    )


async def test_user_flow(hass: HomeAssistant, fake_gitea: FakeGitea) -> None:
    result = await _user_flow(hass, f"{fake_gitea.url}/api/v1/")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "alice @ 127.0.0.1"
    assert result["data"] == {
        CONF_URL: fake_gitea.url,
        CONF_API_TOKEN: TOKEN,
        CONF_VERIFY_SSL: True,
    }
    assert result["options"] == {
        CONF_SCAN_INTERVAL: 15,
        CONF_COMPLETED_DAYS: 7,
        CONF_ALLOW_CLOSING: False,
    }
    assert result["result"].unique_id == f"{fake_gitea.url}#1"


@pytest.mark.parametrize(
    ("failure", "token", "error"),
    [
        (None, "wrong", "invalid_auth"),
        (Failure("/user", 403), TOKEN, "missing_scope"),
        (Failure("/user", 404), TOKEN, "invalid_url"),
        (Failure("/user", 0), TOKEN, "invalid_url"),
        (Failure("/user", 500), TOKEN, "cannot_connect"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    fake_gitea: FakeGitea,
    failure: Failure | None,
    token: str,
    error: str,
) -> None:
    if failure:
        fake_gitea.failures.append(failure)
    result = await _user_flow(hass, fake_gitea.url, token)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    # The form keeps what was entered and the flow can still finish.
    fake_gitea.failures.clear()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: fake_gitea.url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    result = await _user_flow(hass, "http://127.0.0.1:1")
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_invalid_url(hass: HomeAssistant) -> None:
    result = await _user_flow(hass, "ftp://example.com")
    assert result["errors"] == {"base": "invalid_url"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    config_entry.add_to_hass(hass)
    result = await _user_flow(hass, fake_gitea.url)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_second_user(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    config_entry.add_to_hass(hass)
    result = await _user_flow(hass, fake_gitea.url, OTHER_TOKEN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "bob @ 127.0.0.1"


async def test_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    result = await init_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    fake_gitea.users["new-token"] = fake_gitea.users[TOKEN]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert init_integration.data[CONF_API_TOKEN] == "new-token"
    assert init_integration.state is ConfigEntryState.LOADED


async def test_reauth_after_failed_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    del fake_gitea.users[TOKEN]
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    flow = hass.config_entries.flow.async_progress()[0]
    fake_gitea.users["new-token"] = {"id": 1, "login": "alice"}
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_TOKEN: "new-token"}
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED


async def test_reauth_wrong_account(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await init_integration.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: OTHER_TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert init_integration.data[CONF_API_TOKEN] == TOKEN


async def _reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, data: dict[str, Any]
) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure"
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def test_reconfigure(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    new_url = f"{fake_gitea.url}/gitea"
    result = await _reconfigure(
        hass,
        init_integration,
        {CONF_URL: f"{new_url}/", CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert init_integration.data[CONF_URL] == new_url
    assert init_integration.data[CONF_VERIFY_SSL] is False
    assert init_integration.unique_id == f"{new_url}#1"
    assert init_integration.state is ConfigEntryState.LOADED


async def test_reconfigure_error_then_success(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    result = await _reconfigure(
        hass,
        init_integration,
        {CONF_URL: fake_gitea.url, CONF_API_TOKEN: "wrong", CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: fake_gitea.url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_wrong_account(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    result = await _reconfigure(
        hass,
        init_integration,
        {CONF_URL: fake_gitea.url, CONF_API_TOKEN: OTHER_TOKEN, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "wrong_account"


async def test_reconfigure_clashes_with_other_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    new_url = f"{fake_gitea.url}/gitea"
    MockConfigEntry(domain=DOMAIN, unique_id=f"{new_url}#1").add_to_hass(hass)
    result = await _reconfigure(
        hass,
        init_integration,
        {CONF_URL: new_url, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    assert result["reason"] == "already_configured"


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 30.0, CONF_COMPLETED_DAYS: 0.0, CONF_ALLOW_CLOSING: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options == {
        CONF_SCAN_INTERVAL: 30,
        CONF_COMPLETED_DAYS: 0,
        CONF_ALLOW_CLOSING: True,
    }
    coordinator = init_integration.runtime_data.repositories[SUBENTRY_ID]
    assert coordinator.update_interval.total_seconds() == 1800


async def _start_subentry(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_REPOSITORY), context={"source": SOURCE_USER}
    )


async def test_add_repository(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await _start_subentry(hass, init_integration)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    # Already added and issue-less repositories aren't offered.
    selector = result["data_schema"].schema[CONF_REPOSITORY]
    assert selector.config["options"] == ["alice/notes"]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: " Alice/Notes "}
    )
    assert result["step_id"] == "labels"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_INCLUDE_LABELS: [" todo ", "todo", ""]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "alice/notes"
    assert result["unique_id"] == "8"
    assert result["data"] == {
        CONF_REPOSITORY: "alice/notes",
        CONF_REPOSITORY_ID: 8,
        CONF_INCLUDE_LABELS: ["todo"],
        CONF_EXCLUDE_LABELS: [],
    }
    await hass.async_block_till_done()
    assert hass.states.get("todo.alice_notes_issues") is not None


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("not-a-repo", {CONF_REPOSITORY: "invalid_repository"}),
        ("alice/missing", {CONF_REPOSITORY: "repository_not_found"}),
        ("alice/wiki", {CONF_REPOSITORY: "issues_disabled"}),
    ],
)
async def test_add_repository_errors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    value: str,
    error: dict[str, str],
) -> None:
    result = await _start_subentry(hass, init_integration)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: value}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == error


async def test_add_repository_connection_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    result = await _start_subentry(hass, init_integration)
    fake_gitea.failures.append(Failure("/repos/alice/notes", 502))
    fake_gitea.failures.append(Failure("/repos/search", 502))
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: "alice/notes"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    # The list couldn't be loaded; typing a name still works.
    assert result["data_schema"].schema[CONF_REPOSITORY].config["options"] == []


async def test_add_repository_labels_unavailable(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    result = await _start_subentry(hass, init_integration)
    fake_gitea.failures.append(Failure("/repos/alice/notes/labels", 500))
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: "alice/notes"}
    )
    assert result["step_id"] == "labels"
    assert result["data_schema"].schema[CONF_INCLUDE_LABELS].config["options"] == []


async def test_add_repository_already_configured(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await _start_subentry(hass, init_integration)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_REPOSITORY: "INFRA/metatron"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_add_repository_entry_not_loaded(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start_subentry(hass, config_entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_reconfigure_repository(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.subentries.async_init(
        (init_integration.entry_id, SUBENTRY_TYPE_REPOSITORY),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": SUBENTRY_ID},
    )
    assert result["step_id"] == "reconfigure"
    options = result["data_schema"].schema[CONF_INCLUDE_LABELS].config["options"]
    assert options == ["bug", "Kind/Feature", "wontfix"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_INCLUDE_LABELS: ["bug"], CONF_EXCLUDE_LABELS: []},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert init_integration.subentries[SUBENTRY_ID].data[CONF_INCLUDE_LABELS] == ["bug"]
    # Reloaded with the new filter: only #1 and #9 have the bug label.
    assert hass.states.get("todo.infra_metatron_issues").state == "2"


async def test_reconfigure_repository_offline(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    fake_gitea.failures.append(Failure("/repositories", 502))
    result = await hass.config_entries.subentries.async_init(
        (init_integration.entry_id, SUBENTRY_TYPE_REPOSITORY),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": SUBENTRY_ID},
    )
    # The current filter labels are still offered.
    options = result["data_schema"].schema[CONF_INCLUDE_LABELS].config["options"]
    assert options == ["wontfix"]
