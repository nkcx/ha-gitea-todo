# Gitea To-do for Home Assistant

[![Validate](https://github.com/nkcx/ha-gitea-todo/actions/workflows/validate.yml/badge.svg)](https://github.com/nkcx/ha-gitea-todo/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

Shows the issues of your [Gitea](https://about.gitea.com/) (or
[Forgejo](https://forgejo.org/)) repositories as Home Assistant **to-do lists**.
Keep a repository as your household's or homelab's task tracker, and see what's
open on a dashboard, in the companion app or by asking Assist.

- Each repository you add becomes a **to-do list** on its own device.
- Every **open issue** is an item: `#12 Fix the garage sensor`, with a link to the
  issue, its labels and milestone, and its due date.
- Recently **closed issues** stay on the list as completed items for a few days.
- **Gitea stays the source of truth.** The lists are read-only, unless you allow
  ticking items off, which closes the issue in Gitea.
- Optional **label filters** per repository: only `todo` issues, or everything but
  `wontfix`.

*Independent community project, not affiliated with Gitea Ltd. or Forgejo; see
[License and trademarks](#license-and-trademarks).*

## Requirements

- Home Assistant **2026.9** or newer.
- Gitea **1.20** or newer, or Forgejo (any version with scoped access tokens).
- A personal access token (see [Creating an access token](#creating-an-access-token)).

## Installation

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nkcx&repository=ha-gitea-todo&category=integration)

Or by hand:

1. In HACS, open the menu → **Custom repositories**, add
   `https://github.com/nkcx/ha-gitea-todo` with type **Integration**.
2. Install **Gitea To-do** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Gitea To-do**, or use
   this button:

   [![Open your Home Assistant instance and start setting up Gitea To-do.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=gitea_todo)

4. On the new entry, choose **Add repository** for each repository you want as a
   list.

## Configuration

### Connecting

| Setting | Description |
|---|---|
| URL | Address of your Gitea instance as you open it in a browser, e.g. `https://gitea.example.com` (or `https://example.com/gitea` for a sub-path install). The API address (`…/api/v1`) also works. |
| Access token | See below. |
| Verify SSL certificate | Turn off only for self-signed certificates. |

One entry is one Gitea user on one instance. To use another user's view, add
another entry. The URL and token can be changed later with **Reconfigure**; the
token must belong to the same user.

### Creating an access token

In Gitea, go to **Settings → Applications → Generate new token** and give it these
permissions:

| Scope | Access | Needed for |
|---|---|---|
| `user` | Read | Checking who the token belongs to |
| `repository` | Read | Listing repositories and their labels |
| `issue` | Read | Reading issues |
| `issue` | **Read and write** | Only if you turn on *Allow closing issues* |

Choose **All (public, private, and limited)** repository access if the
repositories are private. Gitea shows the token only once.

### Adding repositories

Use **Add repository** on the entry. Pick a repository from the list (only ones
the token's user can see and that have issues turned on) or type `owner/name`.
Then choose label filters:

| Setting | Description |
|---|---|
| Only issues with these labels | An issue is shown if it has **any** of these labels. Leave empty to show all issues. |
| Hide issues with these labels | An issue with any of these labels is hidden, even if it also has an included label. |

Labels are matched without regard to case. Change them later with the repository's
**Reconfigure** (⋮) menu. Remove a repository with its **Delete** menu; its list
and device go away.

### Options

Under **Configure** on the entry:

| Option | Default | Description |
|---|---|---|
| Update interval | 15 min | How often each repository is checked (1 – 1440 min). |
| Keep closed issues for | 7 days | Closed issues stay on the list as completed items for this many days, newest first. `0` hides closed issues. |
| Allow closing issues from Home Assistant | Off | Ticking an item off closes the issue in Gitea; unticking it reopens it. Needs the `issue` write scope. |

## Devices and entities

Each repository is a **service device** named after the repository
(`owner/name`), with a link to it in Gitea. Its one entity is the
**Issues** to-do list, e.g. `todo.infra_metatron_issues`. The list's state is the
number of open issues shown.

| Item field | Comes from |
|---|---|
| Summary | `#<number> <title>` |
| Status | *Needs action* for open issues, *Completed* for closed ones |
| Due | The issue's due date, or else its milestone's due date |
| Description | The issue's link, then its labels and milestone |
| Completed | When the issue was closed |

Open issues are sorted newest first, like Gitea's issue list; completed items
follow, most recently closed first. Pull requests are never shown.

When a repository is renamed or transferred in Gitea, the list follows it (it is
tracked by its ID) and the device's name and link are updated.

## Examples

Show a repository's list on a dashboard:

```yaml
type: todo-list
entity: todo.infra_metatron_issues
```

Notify when a new issue appears (the state counts open issues):

```yaml
triggers:
  - trigger: state
    entity_id: todo.infra_metatron_issues
conditions:
  - condition: template
    value_template: >
      {{ trigger.from_state.state | int(0) < trigger.to_state.state | int(0) }}
actions:
  - action: notify.mobile_app_phone
    data:
      message: "New issue in infra/metatron"
```

Read the open issues in a script, e.g. for a morning briefing:

```yaml
actions:
  - action: todo.get_items
    target:
      entity_id: todo.infra_metatron_issues
    data:
      status: needs_action
    response_variable: issues
```

## How data is updated

Every repository is checked on the update interval (default 15 minutes): the
repository itself (to follow renames), its open issues and, if closed issues are
kept, the issues closed within that many days. Closing or reopening an item from
Home Assistant updates the list straight away. Changes made in Gitea show up at the
next check; use **Reload** on the entry to check now.

## Known limitations

- Only an item's status can be changed from Home Assistant, and only when
  *Allow closing issues* is on. Items can't be added, renamed, deleted or
  reordered; do that in Gitea.
- Changes in Gitea are picked up by polling, not webhooks.
- Very large repositories are read up to 5,000 open issues per check.
- Due dates are shown as dates (Gitea's due dates have no meaningful time).

## Troubleshooting

- **The access token was rejected** / *reauthentication required*: the token was
  deleted or expired; Home Assistant asks for a new one. It must belong to the same
  Gitea user.
- **The token works but lacks the read:user scope**: create a token with the
  scopes listed in [Creating an access token](#creating-an-access-token).
- **This doesn't look like a Gitea instance**: use the address you open in a
  browser, without a repository path.
- **Repository not found** repair: the repository was deleted or the user lost
  access. Remove it from the entry, or restore access; the repair clears at the
  next check.
- **Gitea refused to change the issue**: the token needs the `issue` write scope
  and the user write access to the repository's issues.
- **A list is unavailable**: the last check failed; the reason is in the log and
  in the diagnostics.
- Download **diagnostics** from the entry when reporting an issue; the token is
  redacted.

## Removal

Go to **Settings → Devices & services → Gitea To-do → ⋮ → Delete**, then remove
the repository from HACS. Nothing is changed in Gitea; you can delete the access
token there afterwards.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest tests
.venv/bin/ruff check custom_components tests && .venv/bin/mypy
```

The tests run the integration against a fake Gitea API server.

## License and trademarks

This integration is licensed under the [MIT License](LICENSE).

This is an independent, community project. It is **not affiliated with, endorsed
by, or sponsored by Gitea Ltd. or the Forgejo project**. Gitea and Forgejo are
trademarks of their respective owners and are used here only to identify the
software this integration works with. The integration's icon is its own and does
not use either project's logo.
