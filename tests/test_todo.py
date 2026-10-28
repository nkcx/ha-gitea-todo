"""Tests for the to-do list entity."""

from datetime import date, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.todo import DOMAIN as TODO_DOMAIN, TodoServices
from homeassistant.components.todo.const import (
    ATTR_DESCRIPTION,
    ATTR_DUE_DATE,
    ATTR_ITEM,
    ATTR_RENAME,
    ATTR_STATUS,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.gitea_todo.api import Issue
from custom_components.gitea_todo.const import (
    CONF_ALLOW_CLOSING,
    CONF_EXCLUDE_LABELS,
    CONF_INCLUDE_LABELS,
    DOMAIN,
)
from custom_components.gitea_todo.todo import issue_to_item

from .conftest import SUBENTRY_ID
from .fake_gitea import Failure, FakeGitea

ENTITY_ID = "todo.infra_metatron_issues"


async def _items(hass: HomeAssistant) -> list[dict[str, Any]]:
    result = await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.GET_ITEMS,
        {ATTR_ENTITY_ID: ENTITY_ID},
        blocking=True,
        return_response=True,
    )
    items: list[dict[str, Any]] = result[ENTITY_ID]["items"]  # type: ignore[index]
    return items


async def test_items(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    state = hass.states.get(ENTITY_ID)
    assert state.state == "3"  # open issues: #1, #2, #3 (#9 is excluded)
    assert not state.attributes.get(ATTR_SUPPORTED_FEATURES)
    items = await _items(hass)
    assert [(item["uid"], item["status"]) for item in items] == [
        ("3", TodoItemStatus.NEEDS_ACTION),
        ("2", TodoItemStatus.NEEDS_ACTION),
        ("1", TodoItemStatus.NEEDS_ACTION),
        # #8 was updated recently but closed long ago; #6 is too old.
        ("5", TodoItemStatus.COMPLETED),
    ]
    by_uid = {item["uid"]: item for item in items}
    assert by_uid["1"]["summary"] == "#1 First"
    assert by_uid["1"]["description"] == (
        f"{fake_gitea.url}/infra/metatron/issues/1\nLabels: bug"
    )
    assert "due" not in by_uid["1"]
    # The issue's own deadline wins over its milestone's.
    assert by_uid["3"]["due"] == "2026-10-30"
    assert by_uid["3"]["description"] == (
        f"{fake_gitea.url}/infra/metatron/issues/3\nLabels: Kind/Feature\nMilestone: v1"
    )
    assert by_uid["2"]["due"] == "2026-11-01"


@pytest.mark.parametrize(
    "repository_data",
    [
        {
            "repository": "infra/metatron",
            "repository_id": 7,
            CONF_INCLUDE_LABELS: ["BUG", "kind/feature"],
            CONF_EXCLUDE_LABELS: [],
        }
    ],
)
async def test_include_labels(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert [item["uid"] for item in await _items(hass)] == ["9", "3", "1"]


@pytest.mark.parametrize(
    "repository_data", [{"repository": "infra/metatron", "repository_id": 7}]
)
async def test_no_filters(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert hass.states.get(ENTITY_ID).state == "4"


@pytest.mark.parametrize(
    "entry_options",
    [{"scan_interval": 15, "completed_days": 0, CONF_ALLOW_CLOSING: False}],
)
async def test_completed_hidden(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    assert [item["uid"] for item in await _items(hass)] == ["3", "2", "1"]
    assert not any(
        "state=closed" in path or path.endswith("closed")
        for _, path in fake_gitea.requests
    )


def test_issue_to_item_closed() -> None:
    closed = datetime(2026, 10, 1, 8, 30).astimezone()
    issue = Issue(
        id=1,
        number=12,
        title="Done",
        html_url="https://g/x/y/issues/12",
        is_open=False,
        labels=(),
        milestone=None,
        due=None,
        closed_at=closed,
        updated_at=closed,
    )
    item = issue_to_item(issue)
    assert item.status == TodoItemStatus.COMPLETED
    assert item.completed == closed
    assert item.description == "https://g/x/y/issues/12"


async def test_repository_renamed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_gitea: FakeGitea,
    device_registry: dr.DeviceRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_gitea.repos[7].full_name = "infra/metatron-ha"
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{fake_gitea.url}#1_7"), init_integration.entry_id
    )
    assert device is not None
    assert device.name == "infra/metatron-ha"
    assert device.configuration_url == f"{fake_gitea.url}/infra/metatron-ha"
    items = await _items(hass)
    assert items[0]["description"].startswith(
        f"{fake_gitea.url}/infra/metatron-ha/issues/"
    )


async def test_read_only_rejects_updates(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            TODO_DOMAIN,
            TodoServices.UPDATE_ITEM,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_ITEM: "1", ATTR_STATUS: "completed"},
            blocking=True,
        )


ALLOW = [{"scan_interval": 15, "completed_days": 7, CONF_ALLOW_CLOSING: True}]


async def _update(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.UPDATE_ITEM,
        {ATTR_ENTITY_ID: ENTITY_ID, **data},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("entry_options", ALLOW)
async def test_close_and_reopen(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    state = hass.states.get(ENTITY_ID)
    assert (
        state.attributes[ATTR_SUPPORTED_FEATURES]
        == TodoListEntityFeature.UPDATE_TODO_ITEM
    )
    await _update(hass, **{ATTR_ITEM: "1", ATTR_STATUS: "completed"})
    assert ("PATCH", "/repos/infra/metatron/issues/1") in fake_gitea.requests
    assert (
        next(i for i in fake_gitea.repos[7].issues if i.number == 1).state == "closed"
    )
    assert hass.states.get(ENTITY_ID).state == "2"
    items = {item["uid"]: item for item in await _items(hass)}
    assert items["1"]["status"] == TodoItemStatus.COMPLETED

    await _update(hass, **{ATTR_ITEM: "#1 First", ATTR_STATUS: "needs_action"})
    assert hass.states.get(ENTITY_ID).state == "3"


@pytest.mark.parametrize("entry_options", ALLOW)
async def test_same_status_is_noop(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    await _update(hass, **{ATTR_ITEM: "1", ATTR_STATUS: "needs_action"})
    assert not any(method == "PATCH" for method, _ in fake_gitea.requests)


@pytest.mark.parametrize("entry_options", ALLOW)
@pytest.mark.parametrize(
    "data",
    [
        {ATTR_RENAME: "New title"},
        {ATTR_DESCRIPTION: "Changed"},
        {ATTR_DUE_DATE: date(2027, 1, 1)},
    ],
)
async def test_only_status_can_change(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_gitea: FakeGitea,
    data: dict[str, Any],
) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _update(hass, **{ATTR_ITEM: "1", **data})
    # Description and due date are rejected by Home Assistant itself, as the
    # list doesn't support setting them; a rename reaches the entity.
    assert err.value.translation_key in ("only_status", "update_field_not_supported")
    assert not any(method == "PATCH" for method, _ in fake_gitea.requests)


@pytest.mark.parametrize("entry_options", ALLOW)
@pytest.mark.parametrize(
    ("failure", "translation_key"),
    [
        (
            Failure("/repos/infra/metatron/issues/1", 403, method="PATCH"),
            "no_write_permission",
        ),
        (
            Failure("/repos/infra/metatron/issues/1", 404, method="PATCH"),
            "item_not_found",
        ),
        (
            Failure("/repos/infra/metatron/issues/1", 500, method="PATCH"),
            "update_failed",
        ),
    ],
)
async def test_close_errors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_gitea: FakeGitea,
    failure: Failure,
    translation_key: str,
) -> None:
    fake_gitea.failures.append(failure)
    with pytest.raises(HomeAssistantError) as err:
        await _update(hass, **{ATTR_ITEM: "1", ATTR_STATUS: "completed"})
    assert err.value.translation_key == translation_key


@pytest.mark.parametrize("entry_options", ALLOW)
async def test_update_unknown_item(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entity = hass.data["entity_components"][TODO_DOMAIN].get_entity(ENTITY_ID)
    from homeassistant.components.todo import TodoItem  # noqa: PLC0415

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_update_todo_item(
            TodoItem(uid="999", status=TodoItemStatus.COMPLETED)
        )
    assert err.value.translation_key == "item_not_found"


@pytest.mark.parametrize("entry_options", ALLOW)
async def test_update_without_status(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    entity = hass.data["entity_components"][TODO_DOMAIN].get_entity(ENTITY_ID)
    current = next(i for i in entity.todo_items if i.uid == "1")
    from dataclasses import replace  # noqa: PLC0415

    await entity.async_update_todo_item(replace(current, status=None))
    assert not any(method == "PATCH" for method, _ in fake_gitea.requests)


async def test_unavailable_on_first_refresh_failure(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    fake_gitea.failures.append(Failure("/repositories/7", 500))
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.state == "unavailable"
    coordinator = config_entry.runtime_data.repositories[SUBENTRY_ID]
    assert coordinator.data is None
    # The device falls back to the configured name until the first success.
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{fake_gitea.url}#1_7"), config_entry.entry_id
    )
    assert device is not None
    assert device.name == "infra/metatron"
    assert device.configuration_url is None

    # Further failures leave it unavailable.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "unavailable"


@pytest.mark.parametrize(
    ("status", "translation_key"),
    [(403, "permission_denied"), (502, "cannot_connect")],
)
async def test_update_errors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    fake_gitea: FakeGitea,
    freezer: FrozenDateTimeFactory,
    status: int,
    translation_key: str,
) -> None:
    fake_gitea.failures.append(Failure("/repos/infra/metatron/issues", status))
    coordinator = init_integration.runtime_data.repositories[SUBENTRY_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "unavailable"
    assert coordinator.last_exception.translation_key == translation_key


@pytest.mark.parametrize(
    "entry_options",
    [{"scan_interval": 15, "completed_days": 0, CONF_ALLOW_CLOSING: True}],
)
async def test_close_without_completed_items(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await _update(hass, **{ATTR_ITEM: "1", ATTR_STATUS: "completed"})
    assert [item["uid"] for item in await _items(hass)] == ["3", "2"]


@pytest.mark.parametrize("entry_options", ALLOW)
@pytest.mark.parametrize(
    "repository_data",
    [
        {
            "repository": "infra/metatron",
            "repository_id": 7,
            CONF_INCLUDE_LABELS: [],
            CONF_EXCLUDE_LABELS: ["bug"],
        }
    ],
)
async def test_closed_issue_filtered_out(
    hass: HomeAssistant, init_integration: MockConfigEntry, fake_gitea: FakeGitea
) -> None:
    # Labels changed in Gitea since the last refresh: the answer is filtered too.
    fake_gitea.repos[7].issues[1].labels = ["bug"]
    await _update(hass, **{ATTR_ITEM: "2", ATTR_STATUS: "completed"})
    assert [item["uid"] for item in await _items(hass)] == ["3", "5"]
