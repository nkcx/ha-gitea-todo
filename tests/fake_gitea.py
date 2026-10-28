"""A fake Gitea API (the parts this integration uses), served by aiohttp."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

TOKEN = "0123456789abcdef0123456789abcdef01234567"
OTHER_TOKEN = "fedcba9876543210fedcba9876543210fedcba98"
NOW = datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime) -> str:
    """Format like Gitea."""
    return value.isoformat().replace("+00:00", "Z")


@dataclass
class FakeIssue:
    """An issue (or pull request) in the fake."""

    number: int
    title: str
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    milestone: dict[str, Any] | None = None
    due_date: str | None = None
    closed_at: datetime | None = None
    updated_at: datetime = NOW
    is_pull: bool = False


@dataclass
class FakeRepo:
    """A repository in the fake."""

    id: int
    full_name: str
    has_issues: bool = True
    push: bool = True
    labels: list[str] = field(default_factory=list)
    issues: list[FakeIssue] = field(default_factory=list)


@dataclass
class Failure:
    """Make requests whose path starts with ``path`` fail."""

    path: str
    status: int
    message: str = "boom"
    method: str | None = None


class FakeGitea:
    """State of the fake server."""

    def __init__(self) -> None:
        """Create a server with two users and two repositories."""
        self.url = ""
        self.users = {
            TOKEN: {"id": 1, "login": "alice"},
            OTHER_TOKEN: {"id": 2, "login": "bob"},
        }
        self.version = "1.24.6"
        self.repos: dict[int, FakeRepo] = {
            7: FakeRepo(
                id=7,
                full_name="infra/metatron",
                labels=["bug", "Kind/Feature", "wontfix"],
                issues=[
                    FakeIssue(1, "First", labels=["bug"]),
                    FakeIssue(
                        2,
                        "Second",
                        milestone={"title": "v1", "due_on": "2026-11-01T23:59:59Z"},
                    ),
                    FakeIssue(
                        3,
                        "Third",
                        labels=["Kind/Feature"],
                        due_date="2026-10-30T23:59:59Z",
                        milestone={"title": "v1", "due_on": "2026-11-01T23:59:59Z"},
                    ),
                    FakeIssue(4, "A pull request", is_pull=True),
                    FakeIssue(
                        5,
                        "Closed recently",
                        state="closed",
                        closed_at=NOW - timedelta(days=2),
                        updated_at=NOW - timedelta(days=2),
                    ),
                    FakeIssue(
                        6,
                        "Closed long ago",
                        state="closed",
                        closed_at=NOW - timedelta(days=30),
                        updated_at=NOW - timedelta(days=30),
                    ),
                    FakeIssue(
                        8,
                        "Closed long ago, commented recently",
                        state="closed",
                        closed_at=NOW - timedelta(days=30),
                        updated_at=NOW - timedelta(days=1),
                    ),
                    FakeIssue(9, "Wontfix", labels=["wontfix", "bug"]),
                ],
            ),
            8: FakeRepo(id=8, full_name="alice/notes", push=False),
            9: FakeRepo(id=9, full_name="alice/wiki", has_issues=False),
        }
        self.failures: list[Failure] = []
        self.requests: list[tuple[str, str]] = []

    def repo_json(self, repo: FakeRepo) -> dict[str, Any]:
        """Serialize a repository."""
        return {
            "id": repo.id,
            "full_name": repo.full_name,
            "name": repo.full_name.split("/")[1],
            "owner": {"login": repo.full_name.split("/")[0]},
            "html_url": f"{self.url}/{repo.full_name}",
            "has_issues": repo.has_issues,
            "permissions": {"admin": False, "push": repo.push, "pull": True},
        }

    def issue_json(self, repo: FakeRepo, issue: FakeIssue) -> dict[str, Any]:
        """Serialize an issue."""
        kind = "pulls" if issue.is_pull else "issues"
        return {
            "id": repo.id * 1000 + issue.number,
            "number": issue.number,
            "title": issue.title,
            "html_url": f"{self.url}/{repo.full_name}/{kind}/{issue.number}",
            "state": issue.state,
            "labels": [{"name": name, "color": "ee0701"} for name in issue.labels],
            "milestone": issue.milestone,
            "due_date": issue.due_date,
            "closed_at": iso(issue.closed_at) if issue.closed_at else None,
            "updated_at": iso(issue.updated_at),
            "created_at": iso(NOW - timedelta(days=60)),
            "pull_request": {"merged": False} if issue.is_pull else None,
        }

    def repo_by_name(self, owner: str, name: str) -> FakeRepo | None:
        """Find a repository by owner and name."""
        full_name = f"{owner}/{name}".casefold()
        return next(
            (r for r in self.repos.values() if r.full_name.casefold() == full_name),
            None,
        )


def _paginate(request: web.Request, items: list[Any]) -> web.Response:
    page = int(request.query.get("page", "1"))
    limit = min(int(request.query.get("limit", "30")), 50)
    chunk = items[(page - 1) * limit : page * limit]
    return web.json_response(chunk, headers={"X-Total-Count": str(len(items))})


def make_app(fake: FakeGitea, *, root: bool = True) -> web.Application:
    """Build the aiohttp app of the fake API, also served under ``/gitea``."""

    @web.middleware
    async def middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        path = request.path.removeprefix("/gitea").removeprefix("/api/v1")
        fake.requests.append((request.method, path))
        for failure in fake.failures:
            if path.startswith(failure.path) and failure.method in (
                None,
                request.method,
            ):
                if failure.status == 0:
                    return web.Response(text="<html>not gitea</html>")
                return web.json_response(
                    {"message": failure.message}, status=failure.status
                )
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("token ")
        if token not in fake.users:
            return web.json_response({"message": "user does not exist"}, status=401)
        request["user"] = fake.users[token]
        return await handler(request)

    routes = web.RouteTableDef()

    @routes.get("/api/v1/version")
    async def version(request: web.Request) -> web.Response:
        return web.json_response({"version": fake.version})

    @routes.get("/api/v1/user")
    async def user(request: web.Request) -> web.Response:
        return web.json_response(request["user"])

    @routes.get("/api/v1/repos/search")
    async def search(request: web.Request) -> web.Response:
        repos = sorted(fake.repos.values(), key=lambda r: r.full_name)
        page = int(request.query.get("page", "1"))
        limit = int(request.query.get("limit", "30"))
        chunk = repos[(page - 1) * limit : page * limit]
        return web.json_response(
            {"ok": True, "data": [fake.repo_json(r) for r in chunk]},
            headers={"X-Total-Count": str(len(repos))},
        )

    @routes.get("/api/v1/repositories/{id}")
    async def repo_by_id(request: web.Request) -> web.Response:
        repo = fake.repos.get(int(request.match_info["id"]))
        if repo is None:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response(fake.repo_json(repo))

    @routes.get("/api/v1/repos/{owner}/{name}")
    async def repo(request: web.Request) -> web.Response:
        found = fake.repo_by_name(
            request.match_info["owner"], request.match_info["name"]
        )
        if found is None:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response(fake.repo_json(found))

    @routes.get("/api/v1/repos/{owner}/{name}/labels")
    async def labels(request: web.Request) -> web.Response:
        found = fake.repo_by_name(
            request.match_info["owner"], request.match_info["name"]
        )
        if found is None:
            return web.json_response({"message": "not found"}, status=404)
        return _paginate(
            request, [{"id": i, "name": n} for i, n in enumerate(found.labels)]
        )

    @routes.get("/api/v1/repos/{owner}/{name}/issues")
    async def issues(request: web.Request) -> web.Response:
        found = fake.repo_by_name(
            request.match_info["owner"], request.match_info["name"]
        )
        if found is None:
            return web.json_response({"message": "not found"}, status=404)
        state = request.query.get("state", "open")
        kind = request.query.get("type")
        since = request.query.get("since")
        selected = [
            issue
            for issue in sorted(found.issues, key=lambda i: -i.number)
            if state in ("all", issue.state)
            and (kind != "issues" or not issue.is_pull)
            and (since is None or issue.updated_at >= datetime.fromisoformat(since))
        ]
        return _paginate(request, [fake.issue_json(found, i) for i in selected])

    @routes.patch("/api/v1/repos/{owner}/{name}/issues/{number}")
    async def edit_issue(request: web.Request) -> web.Response:
        found = fake.repo_by_name(
            request.match_info["owner"], request.match_info["name"]
        )
        if found is None:
            return web.json_response({"message": "not found"}, status=404)
        if not found.push:
            return web.json_response({"message": "forbidden"}, status=403)
        number = int(request.match_info["number"])
        issue = next((i for i in found.issues if i.number == number), None)
        if issue is None:
            return web.json_response({"message": "not found"}, status=404)
        body = await request.json()
        issue.state = body["state"]
        issue.closed_at = NOW if issue.state == "closed" else None
        issue.updated_at = NOW
        return web.json_response(fake.issue_json(found, issue), status=201)

    app = web.Application(middlewares=[middleware] if root else [])
    app.add_routes(routes)
    if root:
        app.add_subapp("/gitea/", make_app(fake, root=False))
    return app
