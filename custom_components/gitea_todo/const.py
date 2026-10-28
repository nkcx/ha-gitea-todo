"""Constants for the Gitea To-do integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "gitea_todo"

SUBENTRY_TYPE_REPOSITORY: Final = "repository"

# Config entry data
CONF_API_TOKEN: Final = "api_token"

# Options
CONF_COMPLETED_DAYS: Final = "completed_days"
CONF_ALLOW_CLOSING: Final = "allow_closing"
DEFAULT_SCAN_INTERVAL: Final = 15
"""Minutes between refreshes of each repository."""
MIN_SCAN_INTERVAL: Final = 1
MAX_SCAN_INTERVAL: Final = 1440
DEFAULT_COMPLETED_DAYS: Final = 7
"""Closed issues stay on the list as completed items for this many days."""
MAX_COMPLETED_DAYS: Final = 365
DEFAULT_ALLOW_CLOSING: Final = False

# Repository subentry data
CONF_REPOSITORY: Final = "repository"
CONF_REPOSITORY_ID: Final = "repository_id"
CONF_INCLUDE_LABELS: Final = "include_labels"
CONF_EXCLUDE_LABELS: Final = "exclude_labels"

ISSUE_REPOSITORY_NOT_FOUND: Final = "repository_not_found"
