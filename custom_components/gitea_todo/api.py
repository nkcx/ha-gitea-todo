"""Async client for the Gitea REST API (also works with Forgejo).

The client has no Home Assistant dependencies; it only needs an
``aiohttp.ClientSession``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

API_PATH = "api/v1"
DEFAULT_TIMEOUT = 30.0
PAGE_SIZE = 50
"""Gitea's default maximum page size (``[api] MAX_RESPONSE_ITEMS``)."""
MAX_PAGES = 100
"""Stop paging after this many pages (5,000 issues) to bound one refresh."""


class GiteaError(Exception):
    """Base class for all Gitea client errors."""


class GiteaConnectionError(GiteaError):
    """The Gitea server could not be reached."""


class GiteaInvalidResponseError(GiteaError):
    """The server did not answer like the Gitea API."""


class GiteaApiError(GiteaError):
    """Gitea answered with an HTTP error."""

    def __init__(self, status: int, message: str) -> None:
        """Initialize the error."""
        super().__init__(f"HTTP {status}: {message}".strip())
        self.status = status
        self.message = message


class GiteaAuthError(GiteaApiError):
    """The token is missing, invalid or revoked (HTTP 401)."""


class GiteaPermissionError(GiteaApiError):
    """The token lacks a scope or the user lacks access (HTTP 403)."""


class GiteaNotFoundError(GiteaApiError):
    """The repository or issue doesn't exist or isn't visible (HTTP 404)."""


def normalize_url(url: str) -> str:
    """Return the base URL of a Gitea instance, without a trailing slash.

    Accepts ``host``, ``https://host``, ``https://host/gitea/`` (sub-path
    installs) and the API URL ``https://host/api/v1``.
    """
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    parsed = URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"Invalid URL: {url}")
    path = parsed.path.rstrip("/").removesuffix(f"/{API_PATH}").rstrip("/")
    return str(
        URL.build(
            scheme=parsed.scheme.lower(),
            host=parsed.host.lower(),
            port=parsed.explicit_port,
            path=path,
        )
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class User:
    """The user a token belongs to."""

    id: int
    login: str

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> User:
        """Create from an API user object."""
        return cls(id=int(data["id"]), login=str(data["login"]))


@dataclass(frozen=True, slots=True)
class Repository:
    """A repository."""

    id: int
    full_name: str
    html_url: str
    has_issues: bool
    can_write: bool

    @property
    def owner(self) -> str:
        """The repository's owner (user or organization)."""
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        """The repository's name without its owner."""
        return self.full_name.split("/", 1)[1]

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Repository:
        """Create from an API repository object."""
        permissions = data.get("permissions") or {}
        return cls(
            id=int(data["id"]),
            full_name=str(data["full_name"]),
            html_url=str(data.get("html_url", "")),
            has_issues=bool(data.get("has_issues", True)),
            can_write=bool(permissions.get("push") or permissions.get("admin")),
        )


@dataclass(frozen=True, slots=True)
class Issue:
    """An issue (pull requests are never turned into issues)."""

    id: int
    number: int
    title: str
    html_url: str
    is_open: bool
    labels: tuple[str, ...]
    milestone: str | None
    due: datetime | None
    """The issue's own deadline, or else its milestone's due date."""
    closed_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Issue:
        """Create from an API issue object."""
        milestone = data.get("milestone") or {}
        return cls(
            id=int(data["id"]),
            number=int(data["number"]),
            title=str(data.get("title", "")),
            html_url=str(data.get("html_url", "")),
            is_open=data.get("state") == "open",
            labels=tuple(
                str(label["name"]) for label in data.get("labels") or () if label
            ),
            milestone=str(milestone["title"]) if milestone.get("title") else None,
            due=_parse_datetime(data.get("due_date"))
            or _parse_datetime(milestone.get("due_on")),
            closed_at=_parse_datetime(data.get("closed_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )


class GiteaClient:
    """A minimal typed client for the parts of the Gitea API this integration uses."""

    def __init__(
        self,
        url: str,
        token: str,
        session: aiohttp.ClientSession,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a client for the instance at ``url`` (see ``normalize_url``)."""
        self.url = normalize_url(url)
        self._api = URL(f"{self.url}/{API_PATH}")
        self._token = token
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        """Send a request and return the decoded JSON body and the headers."""
        url = self._api.joinpath(path.lstrip("/"), encoded=False)
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status >= 400:
                    message = await _error_message(response)
                    raise _classify(response.status, message)
                try:
                    body = await response.json(content_type=None)
                except ValueError as err:
                    raise GiteaInvalidResponseError(
                        f"{method} {path}: response is not JSON"
                    ) from err
                return body, response.headers
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GiteaConnectionError(f"{method} {path}: {err!r}") from err

    async def _get_object(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> Mapping[str, Any]:
        body, _ = await self._request("GET", path, params=params)
        if not isinstance(body, Mapping):
            raise GiteaInvalidResponseError(f"GET {path}: expected an object")
        return body

    async def _get_pages(
        self, path: str, params: Mapping[str, str | int] | None = None
    ) -> list[Mapping[str, Any]]:
        """Return every item of a paginated list endpoint."""
        results: list[Mapping[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            body, headers = await self._request(
                "GET", path, params={**(params or {}), "page": page, "limit": PAGE_SIZE}
            )
            if isinstance(body, Mapping) and isinstance(body.get("data"), list):
                body = body["data"]  # /repos/search wraps its list
            if not isinstance(body, list):
                raise GiteaInvalidResponseError(f"GET {path}: expected a list")
            results.extend(item for item in body if isinstance(item, Mapping))
            total = headers.get("X-Total-Count")
            if len(body) < PAGE_SIZE or (
                total is not None and total.isdigit() and len(results) >= int(total)
            ):
                break
        else:
            _LOGGER.warning(
                "Stopped reading %s after %s pages of %s items",
                path,
                MAX_PAGES,
                PAGE_SIZE,
            )
        return results

    async def async_get_version(self) -> str:
        """Return the server version, e.g. ``1.24.6`` (Forgejo adds a suffix)."""
        body = await self._get_object("/version")
        return str(body.get("version", ""))

    async def async_get_user(self) -> User:
        """Return the user the token belongs to."""
        return User.from_api(await self._get_object("/user"))

    async def async_get_repository(self, full_name: str) -> Repository:
        """Return a repository by ``owner/name``."""
        owner, _, name = full_name.strip().strip("/").partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"Not an owner/name: {full_name}")
        return Repository.from_api(await self._get_object(f"/repos/{owner}/{name}"))

    async def async_get_repository_by_id(self, repository_id: int) -> Repository:
        """Return a repository by ID; follows renames and transfers."""
        return Repository.from_api(
            await self._get_object(f"/repositories/{repository_id}")
        )

    async def async_get_repositories(self) -> list[Repository]:
        """Return the repositories the token's user can see, issues enabled."""
        items = await self._get_pages("/repos/search", {"sort": "alpha"})
        repositories = [Repository.from_api(item) for item in items]
        return [repository for repository in repositories if repository.has_issues]

    async def async_get_labels(self, repository: Repository) -> list[str]:
        """Return the names of a repository's labels (including org labels)."""
        items = await self._get_pages(
            f"/repos/{repository.owner}/{repository.name}/labels"
        )
        return sorted({str(item["name"]) for item in items}, key=str.casefold)

    async def async_get_issues(
        self, repository: Repository, *, state: str, since: datetime | None = None
    ) -> list[Issue]:
        """Return a repository's issues (never pull requests).

        ``state`` is ``open``, ``closed`` or ``all``. ``since`` only returns
        issues updated at or after that time.
        """
        params: dict[str, str | int] = {"state": state, "type": "issues"}
        if since is not None:
            params["since"] = since.isoformat()
        items = await self._get_pages(
            f"/repos/{repository.owner}/{repository.name}/issues", params
        )
        return [Issue.from_api(item) for item in items if not item.get("pull_request")]

    async def async_set_issue_state(
        self, repository: Repository, number: int, *, is_open: bool
    ) -> Issue:
        """Close or reopen an issue and return it."""
        body, _ = await self._request(
            "PATCH",
            f"/repos/{repository.owner}/{repository.name}/issues/{number}",
            json={"state": "open" if is_open else "closed"},
        )
        if not isinstance(body, Mapping):
            raise GiteaInvalidResponseError("PATCH issue: expected an object")
        return Issue.from_api(body)


async def _error_message(response: aiohttp.ClientResponse) -> str:
    try:
        body = await response.json(content_type=None)
    except ValueError, aiohttp.ClientError:
        return response.reason or ""
    if isinstance(body, Mapping) and body.get("message"):
        return str(body["message"])
    return response.reason or ""


def _classify(status: int, message: str) -> GiteaApiError:
    if status == 401:
        return GiteaAuthError(status, message)
    if status == 403:
        return GiteaPermissionError(status, message)
    if status == 404:
        return GiteaNotFoundError(status, message)
    return GiteaApiError(status, message)


__all__ = [
    "GiteaApiError",
    "GiteaAuthError",
    "GiteaClient",
    "GiteaConnectionError",
    "GiteaError",
    "GiteaInvalidResponseError",
    "GiteaNotFoundError",
    "GiteaPermissionError",
    "Issue",
    "Repository",
    "User",
    "normalize_url",
]
