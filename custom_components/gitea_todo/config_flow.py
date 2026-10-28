"""Config flow for the Gitea To-do integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, override

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol
from yarl import URL

from .api import (
    GiteaAuthError,
    GiteaClient,
    GiteaConnectionError,
    GiteaError,
    GiteaInvalidResponseError,
    GiteaNotFoundError,
    GiteaPermissionError,
    Repository,
    User,
)
from .const import (
    CONF_ALLOW_CLOSING,
    CONF_API_TOKEN,
    CONF_COMPLETED_DAYS,
    CONF_EXCLUDE_LABELS,
    CONF_INCLUDE_LABELS,
    CONF_REPOSITORY,
    CONF_REPOSITORY_ID,
    DEFAULT_ALLOW_CLOSING,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_COMPLETED_DAYS,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SUBENTRY_TYPE_REPOSITORY,
)

_LOGGER = logging.getLogger(__name__)

EXAMPLE_URL = "https://gitea.example.com"

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.URL, autocomplete="url")
)


class FlowError(Exception):
    """A validation error shown in a flow form."""

    def __init__(self, error: str) -> None:
        """Initialize the error."""
        super().__init__(error)
        self.error = error


async def async_validate(
    hass: HomeAssistant, url: str, token: str, verify_ssl: bool
) -> tuple[GiteaClient, User]:
    """Connect and check the token. Raises FlowError."""
    try:
        client = GiteaClient(
            url, token, async_get_clientsession(hass, verify_ssl=verify_ssl)
        )
    except ValueError as err:
        raise FlowError("invalid_url") from err
    try:
        user = await client.async_get_user()
    except GiteaAuthError as err:
        raise FlowError("invalid_auth") from err
    except GiteaPermissionError as err:
        # Gitea answers 403 when the token lacks the read:user scope.
        raise FlowError("missing_scope") from err
    except GiteaConnectionError as err:
        raise FlowError("cannot_connect") from err
    except GiteaInvalidResponseError, GiteaNotFoundError:
        raise FlowError("invalid_url") from None
    except GiteaError as err:
        _LOGGER.debug("Unexpected Gitea answer during validation: %s", err)
        raise FlowError("cannot_connect") from err
    return client, user


def unique_id_for(client: GiteaClient, user: User) -> str:
    """Identify a config entry by instance and user."""
    return f"{client.url}#{user.id}"


def _connect_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): URL_SELECTOR,
            vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
            vol.Required(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
            ): BooleanSelector(),
        }
    )


class GiteaTodoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect to a Gitea instance with an access token."""

    VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> GiteaTodoOptionsFlow:
        """Return the options flow."""
        return GiteaTodoOptionsFlow()

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Repositories are added as subentries."""
        return {SUBENTRY_TYPE_REPOSITORY: RepositorySubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the URL and token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                client, user = await async_validate(
                    self.hass,
                    user_input[CONF_URL],
                    user_input[CONF_API_TOKEN],
                    user_input[CONF_VERIFY_SSL],
                )
            except FlowError as err:
                errors["base"] = err.error
            else:
                await self.async_set_unique_id(unique_id_for(client, user))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{user.login} @ {URL(client.url).host}",
                    data={**user_input, CONF_URL: client.url},
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_COMPLETED_DAYS: DEFAULT_COMPLETED_DAYS,
                        CONF_ALLOW_CLOSING: DEFAULT_ALLOW_CLOSING,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _connect_schema({}), user_input
            ),
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_URL},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token for the same user."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                client, user = await async_validate(
                    self.hass,
                    entry.data[CONF_URL],
                    user_input[CONF_API_TOKEN],
                    entry.data[CONF_VERIFY_SSL],
                )
            except FlowError as err:
                errors["base"] = err.error
            else:
                await self.async_set_unique_id(unique_id_for(client, user))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self._update_and_finish(
                    entry, {CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
            description_placeholders={"url": entry.data[CONF_URL]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL, token or certificate check of the same account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                client, user = await async_validate(
                    self.hass,
                    user_input[CONF_URL],
                    user_input[CONF_API_TOKEN],
                    user_input[CONF_VERIFY_SSL],
                )
            except FlowError as err:
                errors["base"] = err.error
            else:
                # The URL is part of the unique ID; accept a new URL for the
                # same user (e.g. a new domain) but never a different user.
                if user.id != _user_id(entry):
                    return self.async_abort(reason="wrong_account")
                new_unique_id = unique_id_for(client, user)
                for other in self._async_current_entries(include_ignore=False):
                    if (
                        other.unique_id == new_unique_id
                        and other.entry_id != entry.entry_id
                    ):
                        return self.async_abort(reason="already_configured")
                return self._update_and_finish(
                    entry,
                    {**user_input, CONF_URL: client.url},
                    unique_id=new_unique_id,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _connect_schema(entry.data), user_input
            ),
            errors=errors,
            description_placeholders={"example_url": EXAMPLE_URL},
        )

    def _update_and_finish(
        self,
        entry: ConfigEntry,
        data_updates: Mapping[str, Any],
        unique_id: str | None = None,
    ) -> ConfigFlowResult:
        """Save the entry and make sure it's reloaded.

        A loaded entry reloads through its update listener; one that failed to
        set up (e.g. an expired token) has none, so it's reloaded here.
        """
        if entry.state is ConfigEntryState.LOADED:
            return self.async_update_and_abort(
                entry,
                data_updates=data_updates,
                unique_id=unique_id if unique_id else entry.unique_id,
            )
        return self.async_update_reload_and_abort(
            entry,
            data_updates=data_updates,
            unique_id=unique_id if unique_id else entry.unique_id,
        )


def _user_id(entry: ConfigEntry) -> int | None:
    unique_id = entry.unique_id or ""
    _, _, user_id = unique_id.rpartition("#")
    return int(user_id) if user_id.isdigit() else None


class GiteaTodoOptionsFlow(OptionsFlow):
    """Change how often and what is shown."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_COMPLETED_DAYS: int(user_input[CONF_COMPLETED_DAYS]),
                    CONF_ALLOW_CLOSING: user_input[CONF_ALLOW_CLOSING],
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(
                    CONF_COMPLETED_DAYS,
                    default=options.get(CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=MAX_COMPLETED_DAYS,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="d",
                    )
                ),
                vol.Required(
                    CONF_ALLOW_CLOSING,
                    default=options.get(CONF_ALLOW_CLOSING, DEFAULT_ALLOW_CLOSING),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _label_selector(labels: list[str]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=labels,
            multiple=True,
            custom_value=True,
            sort=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _labels_schema(labels: list[str], defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_INCLUDE_LABELS, default=list(defaults.get(CONF_INCLUDE_LABELS, []))
            ): _label_selector(labels),
            vol.Optional(
                CONF_EXCLUDE_LABELS, default=list(defaults.get(CONF_EXCLUDE_LABELS, []))
            ): _label_selector(labels),
        }
    )


class RepositorySubentryFlow(ConfigSubentryFlow):
    """Add a repository, or change its label filters."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._repository: Repository | None = None
        self._labels: list[str] = []

    def _client(self) -> GiteaClient:
        entry: ConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            raise FlowError("entry_not_loaded")
        client: GiteaClient = entry.runtime_data.client
        return client

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick a repository."""
        errors: dict[str, str] = {}
        try:
            client = self._client()
        except FlowError as err:
            return self.async_abort(reason=err.error)
        if user_input is not None:
            try:
                repository = await client.async_get_repository(
                    user_input[CONF_REPOSITORY]
                )
            except ValueError:
                errors[CONF_REPOSITORY] = "invalid_repository"
            except GiteaNotFoundError:
                errors[CONF_REPOSITORY] = "repository_not_found"
            except GiteaError:
                errors["base"] = "cannot_connect"
            else:
                if not repository.has_issues:
                    errors[CONF_REPOSITORY] = "issues_disabled"
                elif any(
                    subentry.unique_id == str(repository.id)
                    for subentry in self._get_entry().subentries.values()
                ):
                    return self.async_abort(reason="already_configured")
                else:
                    self._repository = repository
                    try:
                        self._labels = await client.async_get_labels(repository)
                    except GiteaError:
                        self._labels = []
                    return await self.async_step_labels()

        try:
            repositories = await client.async_get_repositories()
        except GiteaError:
            repositories = []
        configured = {
            subentry.unique_id for subentry in self._get_entry().subentries.values()
        }
        options = [
            repository.full_name
            for repository in repositories
            if str(repository.id) not in configured
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_REPOSITORY): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        sort=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_labels(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose which labels to include or exclude."""
        repository = self._repository
        assert repository is not None
        if user_input is not None:
            return self.async_create_entry(
                title=repository.full_name,
                data={
                    CONF_REPOSITORY: repository.full_name,
                    CONF_REPOSITORY_ID: repository.id,
                    CONF_INCLUDE_LABELS: _clean(user_input.get(CONF_INCLUDE_LABELS)),
                    CONF_EXCLUDE_LABELS: _clean(user_input.get(CONF_EXCLUDE_LABELS)),
                },
                unique_id=str(repository.id),
            )
        return self.async_show_form(
            step_id="labels",
            data_schema=_labels_schema(self._labels, {}),
            description_placeholders={"repository": repository.full_name},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a repository's label filters."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={
                    CONF_INCLUDE_LABELS: _clean(user_input.get(CONF_INCLUDE_LABELS)),
                    CONF_EXCLUDE_LABELS: _clean(user_input.get(CONF_EXCLUDE_LABELS)),
                },
            )
        labels: list[str] = []
        try:
            client = self._client()
            repository = await client.async_get_repository_by_id(
                subentry.data[CONF_REPOSITORY_ID]
            )
            labels = await client.async_get_labels(repository)
        except FlowError, GiteaError:
            pass
        labels = sorted(
            {
                *labels,
                *subentry.data.get(CONF_INCLUDE_LABELS, ()),
                *subentry.data.get(CONF_EXCLUDE_LABELS, ()),
            },
            key=str.casefold,
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_labels_schema(labels, subentry.data),
            description_placeholders={"repository": subentry.title},
        )


def _clean(labels: list[str] | None) -> list[str]:
    """Strip labels and drop empty ones and duplicates, keeping the order."""
    seen: dict[str, None] = {}
    for label in labels or ():
        if label.strip():
            seen.setdefault(label.strip(), None)
    return list(seen)
