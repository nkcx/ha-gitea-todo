"""Data update coordinator: one per repository."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    GiteaAuthError,
    GiteaClient,
    GiteaError,
    GiteaNotFoundError,
    GiteaPermissionError,
    Issue,
    Repository,
    User,
)
from .const import (
    CONF_COMPLETED_DAYS,
    CONF_EXCLUDE_LABELS,
    CONF_INCLUDE_LABELS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_REPOSITORY_NOT_FOUND,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class GiteaRuntimeData:
    """What a loaded config entry keeps."""

    client: GiteaClient
    user: User
    version: str
    repositories: dict[str, GiteaRepositoryCoordinator] = field(default_factory=dict)
    """Coordinators by repository subentry ID."""


type GiteaConfigEntry = ConfigEntry[GiteaRuntimeData]


@dataclass(frozen=True, slots=True)
class RepositoryData:
    """A repository and the issues to show for it."""

    repository: Repository
    open_issues: list[Issue]
    closed_issues: list[Issue]
    """Issues closed within the retention window, most recently closed first."""


def repair_issue_id(subentry_id: str) -> str:
    """Return the repair issue ID for a repository subentry."""
    return f"{ISSUE_REPOSITORY_NOT_FOUND}_{subentry_id}"


class LabelFilter:
    """Which issues to show, by label (case-insensitive)."""

    def __init__(self, include: Iterable[str], exclude: Iterable[str]) -> None:
        """Show issues with any ``include`` label (all if empty), none of ``exclude``."""
        self.include = {label.casefold() for label in include}
        self.exclude = {label.casefold() for label in exclude}

    def __call__(self, issue: Issue) -> bool:
        """Return whether to show the issue."""
        labels = {label.casefold() for label in issue.labels}
        if self.include and not labels & self.include:
            return False
        return not labels & self.exclude


class GiteaRepositoryCoordinator(DataUpdateCoordinator[RepositoryData]):
    """Fetches one repository's issues."""

    config_entry: GiteaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GiteaConfigEntry,
        client: GiteaClient,
        subentry: ConfigSubentry,
    ) -> None:
        """Track the repository of ``subentry``."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {subentry.data[CONF_REPOSITORY]}",
            update_interval=timedelta(
                minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.subentry_id = subentry.subentry_id
        self.repository_id: int = subentry.data[CONF_REPOSITORY_ID]
        self.configured_name: str = subentry.data[CONF_REPOSITORY]
        self.label_filter = LabelFilter(
            subentry.data.get(CONF_INCLUDE_LABELS, ()),
            subentry.data.get(CONF_EXCLUDE_LABELS, ()),
        )
        self.completed_days: int = entry.options.get(
            CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS
        )

    async def _async_update_data(self) -> RepositoryData:
        try:
            repository = await self.client.async_get_repository_by_id(
                self.repository_id
            )
            open_issues = await self.client.async_get_issues(repository, state="open")
            closed_issues: list[Issue] = []
            if self.completed_days > 0:
                cutoff = dt_util.utcnow() - timedelta(days=self.completed_days)
                closed_issues = [
                    issue
                    for issue in await self.client.async_get_issues(
                        repository, state="closed", since=cutoff
                    )
                    if issue.closed_at is not None and issue.closed_at >= cutoff
                ]
        except GiteaAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except GiteaNotFoundError as err:
            self._create_not_found_issue()
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="repository_not_found",
                translation_placeholders={"repository": self.configured_name},
            ) from err
        except GiteaPermissionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="permission_denied",
                translation_placeholders={
                    "repository": self.configured_name,
                    "error": err.message,
                },
            ) from err
        except GiteaError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err

        ir.async_delete_issue(self.hass, DOMAIN, repair_issue_id(self.subentry_id))
        # Newest issue first (like Gitea's issue list), then the most recently closed.
        open_issues.sort(key=lambda issue: issue.number, reverse=True)
        closed_issues.sort(
            key=lambda issue: issue.closed_at or dt_util.utcnow(), reverse=True
        )
        return RepositoryData(
            repository=repository,
            open_issues=[issue for issue in open_issues if self.label_filter(issue)],
            closed_issues=[
                issue for issue in closed_issues if self.label_filter(issue)
            ],
        )

    @callback
    def async_apply_issue(self, issue: Issue) -> None:
        """Show an issue changed from Home Assistant without waiting for a refresh."""
        data = self.data
        open_issues = [i for i in data.open_issues if i.number != issue.number]
        closed_issues = [i for i in data.closed_issues if i.number != issue.number]
        if self.label_filter(issue):
            if issue.is_open:
                open_issues.append(issue)
                open_issues.sort(key=lambda i: i.number, reverse=True)
            elif self.completed_days > 0:
                closed_issues.insert(0, issue)
        self.async_set_updated_data(
            RepositoryData(
                repository=data.repository,
                open_issues=open_issues,
                closed_issues=closed_issues,
            )
        )

    def _create_not_found_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            repair_issue_id(self.subentry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REPOSITORY_NOT_FOUND,
            translation_placeholders={
                "repository": self.configured_name,
                "entry": self.config_entry.title,
            },
        )
