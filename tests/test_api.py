"""Tests for the Gitea API client."""

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.gitea_todo import api
from custom_components.gitea_todo.api import (
    GiteaApiError,
    GiteaAuthError,
    GiteaClient,
    GiteaConnectionError,
    GiteaInvalidResponseError,
    GiteaNotFoundError,
    GiteaPermissionError,
    Issue,
    Repository,
    normalize_url,
)

from .fake_gitea import NOW, TOKEN, FakeGitea, FakeIssue


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("gitea.example.com", "https://gitea.example.com"),
        ("HTTPS://Gitea.Example.com/", "https://gitea.example.com"),
        ("http://host:3000/api/v1/", "http://host:3000"),
        ("https://host/gitea/", "https://host/gitea"),
        ("https://host/gitea/api/v1", "https://host/gitea"),
        ("  https://host  ", "https://host"),
    ],
)
def test_normalize_url(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


@pytest.mark.parametrize("url", ["ftp://host", "https://", "http:///path"])
def test_normalize_url_invalid(url: str) -> None:
    with pytest.raises(ValueError, match="Invalid URL"):
        normalize_url(url)


@pytest.fixture
async def session(socket_enabled: None) -> AsyncGenerator[ClientSession]:
    """Return an aiohttp session for the client."""
    async with ClientSession() as session:
        yield session


@pytest.fixture
async def client(fake_gitea: FakeGitea, session: ClientSession) -> GiteaClient:
    """Return a client for the fake server."""
    return GiteaClient(fake_gitea.url, TOKEN, session)


async def test_pagination(
    client: GiteaClient, fake_gitea: FakeGitea, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = fake_gitea.repos[7]
    repo.issues = [FakeIssue(n, f"Issue {n}") for n in range(1, 121)]
    repository = await client.async_get_repository("infra/metatron")
    issues = await client.async_get_issues(repository, state="open")
    assert [issue.number for issue in issues] == list(range(120, 0, -1))
    pages = [path for _, path in fake_gitea.requests if path.endswith("/issues")]
    assert len(pages) == 3


async def test_pagination_stops_at_total(
    client: GiteaClient, fake_gitea: FakeGitea
) -> None:
    # Exactly one full page: X-Total-Count says there's nothing more.
    fake_gitea.repos[7].issues = [FakeIssue(n, f"Issue {n}") for n in range(1, 51)]
    repository = await client.async_get_repository("infra/metatron")
    assert len(await client.async_get_issues(repository, state="open")) == 50
    pages = [path for _, path in fake_gitea.requests if path.endswith("/issues")]
    assert len(pages) == 1


async def test_pagination_limit(
    client: GiteaClient,
    fake_gitea: FakeGitea,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(api, "MAX_PAGES", 2)
    fake_gitea.repos[7].issues = [FakeIssue(n, f"Issue {n}") for n in range(1, 151)]
    repository = await client.async_get_repository("infra/metatron")
    assert len(await client.async_get_issues(repository, state="open")) == 100
    assert "Stopped reading" in caplog.text


async def test_since_and_pull_requests(
    client: GiteaClient, fake_gitea: FakeGitea
) -> None:
    repository = await client.async_get_repository("infra/metatron")
    issues = await client.async_get_issues(
        repository, state="closed", since=NOW - timedelta(days=3)
    )
    assert [issue.number for issue in issues] == [8, 5]
    everything = await client.async_get_issues(repository, state="all")
    assert 4 not in [issue.number for issue in everything]


async def test_repositories_and_labels(
    client: GiteaClient, fake_gitea: FakeGitea
) -> None:
    assert await client.async_get_version() == "1.24.6"
    user = await client.async_get_user()
    assert (user.id, user.login) == (1, "alice")
    repositories = await client.async_get_repositories()
    assert [r.full_name for r in repositories] == ["alice/notes", "infra/metatron"]
    notes = repositories[0]
    assert (notes.owner, notes.name, notes.can_write) == ("alice", "notes", False)
    assert await client.async_get_labels(repositories[1]) == [
        "bug",
        "Kind/Feature",
        "wontfix",
    ]
    by_id = await client.async_get_repository_by_id(7)
    assert by_id.full_name == "infra/metatron"


async def test_set_issue_state(client: GiteaClient, fake_gitea: FakeGitea) -> None:
    repository = await client.async_get_repository("infra/metatron")
    issue = await client.async_set_issue_state(repository, 2, is_open=False)
    assert not issue.is_open
    assert issue.closed_at == NOW


@pytest.mark.parametrize("name", ["metatron", "a/b/c", "/", ""])
async def test_get_repository_invalid_name(client: GiteaClient, name: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        await client.async_get_repository(name)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, GiteaAuthError),
        (403, GiteaPermissionError),
        (404, GiteaNotFoundError),
        (500, GiteaApiError),
    ],
)
async def test_http_errors(
    client: GiteaClient,
    fake_gitea: FakeGitea,
    status: int,
    error: type[GiteaApiError],
) -> None:
    from .fake_gitea import Failure  # noqa: PLC0415

    fake_gitea.failures.append(Failure("/user", status, "nope"))
    with pytest.raises(error) as err:
        await client.async_get_user()
    assert err.value.status == status
    assert err.value.message == "nope"


async def _serve(handler: Any) -> TestServer:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    return server


@pytest.mark.parametrize(
    ("response", "call", "error"),
    [
        (web.Response(text="not json"), "user", GiteaInvalidResponseError),
        (web.json_response([1, 2]), "user", GiteaInvalidResponseError),
        (web.json_response({"x": 1}), "labels", GiteaInvalidResponseError),
        (web.json_response([1]), "patch", GiteaInvalidResponseError),
        (web.Response(status=502, text="<html>"), "user", GiteaApiError),
        (web.json_response({"errors": []}, status=502), "user", GiteaApiError),
    ],
)
async def test_invalid_responses(
    session: ClientSession,
    response: web.Response,
    call: str,
    error: type[Exception],
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return response

    server = await _serve(handler)
    client = GiteaClient(str(server.make_url("")), TOKEN, session)
    repository = Repository(1, "a/b", "", has_issues=True, can_write=True)
    calls = {
        "user": client.async_get_user,
        "labels": lambda: client.async_get_labels(repository),
        "patch": lambda: client.async_set_issue_state(repository, 1, is_open=True),
    }
    try:
        with pytest.raises(error) as err:
            await calls[call]()
        if isinstance(err.value, GiteaApiError):
            assert err.value.message == "Bad Gateway"
    finally:
        await server.close()


async def test_connection_error(session: ClientSession) -> None:
    client = GiteaClient("http://127.0.0.1:1", TOKEN, session, timeout=2)
    with pytest.raises(GiteaConnectionError):
        await client.async_get_user()


def test_issue_from_api_defaults() -> None:
    issue = Issue.from_api(
        {
            "id": 1,
            "number": 2,
            "state": "open",
            "labels": None,
            "milestone": {"title": "", "due_on": "garbage"},
            "due_date": None,
        }
    )
    assert issue.title == ""
    assert issue.labels == ()
    assert issue.milestone is None
    assert issue.due is None
    assert issue.closed_at is None
