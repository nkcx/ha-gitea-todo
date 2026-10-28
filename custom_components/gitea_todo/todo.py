"""To-do lists of Gitea issues."""

from __future__ import annotations

from datetime import date

from homeassistant.components.todo import TodoItem, TodoListEntity
from homeassistant.components.todo.const import TodoItemStatus, TodoListEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import GiteaError, GiteaNotFoundError, GiteaPermissionError, Issue
from .const import CONF_ALLOW_CLOSING, DEFAULT_ALLOW_CLOSING, DOMAIN
from .coordinator import GiteaConfigEntry, GiteaRepositoryCoordinator, RepositoryData

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GiteaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one to-do list per repository."""
    allow_closing = entry.options.get(CONF_ALLOW_CLOSING, DEFAULT_ALLOW_CLOSING)
    for subentry_id, coordinator in entry.runtime_data.repositories.items():
        async_add_entities(
            [GiteaIssuesTodoList(coordinator, allow_closing=allow_closing)],
            config_subentry_id=subentry_id,
        )


def issue_to_item(issue: Issue) -> TodoItem:
    """Turn an issue into a to-do item."""
    lines = [issue.html_url]
    if issue.labels:
        lines.append(f"Labels: {', '.join(issue.labels)}")
    if issue.milestone:
        lines.append(f"Milestone: {issue.milestone}")
    due: date | None = None
    if issue.due is not None:
        due = dt_util.as_local(issue.due).date()
    return TodoItem(
        uid=str(issue.number),
        summary=f"#{issue.number} {issue.title}",
        status=TodoItemStatus.NEEDS_ACTION
        if issue.is_open
        else TodoItemStatus.COMPLETED,
        due=due,
        description="\n".join(lines),
        completed=None if issue.is_open else issue.closed_at,
    )


class GiteaIssuesTodoList(
    CoordinatorEntity[GiteaRepositoryCoordinator], TodoListEntity
):
    """A repository's issues as a to-do list."""

    _attr_has_entity_name = True
    _attr_translation_key = "issues"

    def __init__(
        self, coordinator: GiteaRepositoryCoordinator, *, allow_closing: bool
    ) -> None:
        """Show the issues of the coordinator's repository."""
        super().__init__(coordinator)
        runtime = coordinator.config_entry.runtime_data
        server_id = (
            coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        )
        device_key = f"{server_id}_{coordinator.repository_id}"
        self._attr_unique_id = device_key
        repository = coordinator.data.repository if coordinator.data else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_key)},
            name=repository.full_name if repository else coordinator.configured_name,
            manufacturer="Gitea",
            model="Repository",
            sw_version=runtime.version or None,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=repository.html_url if repository else None,
        )
        if allow_closing:
            self._attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM
        self._update_items()

    @callback
    def _update_items(self) -> None:
        # None until the first successful refresh (setup doesn't wait for one).
        data: RepositoryData | None = self.coordinator.data
        if data is None:
            self._attr_todo_items = None
            return
        self._attr_todo_items = [
            issue_to_item(issue) for issue in (*data.open_issues, *data.closed_issues)
        ]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_items()
        self._update_device()
        super()._handle_coordinator_update()

    @callback
    def _update_device(self) -> None:
        """Follow a renamed or transferred repository."""
        entry = self.registry_entry
        if entry is None or (device_id := entry.device_id) is None:  # pragma: no cover
            return  # Never: the entity has a unique ID and a device.
        registry = dr.async_get(self.hass)
        device = registry.async_get(device_id)
        repository = self.coordinator.data.repository
        if isinstance(device, dr.DeviceEntry) and (
            device.name != repository.full_name
            or device.configuration_url != repository.html_url
        ):
            registry.async_update_device(
                device_id,
                name=repository.full_name,
                configuration_url=repository.html_url,
            )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Close or reopen an issue; nothing else can be changed from here."""
        data: RepositoryData | None = self.coordinator.data
        current = next((i for i in self.todo_items or () if i.uid == item.uid), None)
        if data is None or current is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="item_not_found",
                translation_placeholders={"uid": str(item.uid)},
            )
        if (item.summary, item.description, item.due) != (
            current.summary,
            current.description,
            current.due,
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="only_status"
            )
        if item.status is None or item.status == current.status:
            return
        number = int(current.uid or 0)
        try:
            issue = await self.coordinator.client.async_set_issue_state(
                data.repository,
                number,
                is_open=item.status == TodoItemStatus.NEEDS_ACTION,
            )
        except GiteaPermissionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_write_permission",
                translation_placeholders={"repository": data.repository.full_name},
            ) from err
        except GiteaNotFoundError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="item_not_found",
                translation_placeholders={"uid": str(item.uid)},
            ) from err
        except GiteaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_apply_issue(issue)
