"""Config flow for electricity_billing_nz integration."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_DISTRIBUTOR,
    CONF_EFFECTIVE_FROM,
    CONF_EXPORT_FLOOR,
    CONF_FORECAST_ENTITY,
    CONF_GST_INCLUSIVE,
    CONF_ICP,
    CONF_LIVE_ENTITY,
    CONF_LOSS_CATEGORY,
    CONF_NETWORK_OVERRIDES,
    CONF_PLAN_NAME,
    CONF_PRICE_CATEGORY,
    CONF_RETAILER_VERSIONS,
    CONF_SETTLED_ENTITY,
    DOMAIN,
    FIRST_EFFECTIVE_FROM,
    RETAILER_DAILY_FIELDS,
    RETAILER_PER_KWH_FIELDS,
)
from .icp import async_remove_ignored, async_validate_icp
from .networks import (
    OVERRIDE_DAILY,
    OVERRIDE_EXPORT_CREDIT_PREFIX,
    OVERRIDE_IMPORT_PREFIX,
    OVERRIDE_LOSS_FACTOR,
    async_get_networks,
    network_for_entry,
)
from .sources import SOURCE_KEYS, resolve_source, source_id
from .tariff import all_bands, to_nzd_per_kwh

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

    from .networks import Network

# Rounding after c/kWh -> NZD/kWh, so 2.86 is stored as 0.0286.
STORE_DIGITS = 8

SENSOR_SELECTOR = EntitySelector(EntitySelectorConfig(domain="sensor"))
CENTS_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=-100,
        max=100,
        step="any",
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="c/kWh",
    )
)
DOLLARS_PER_DAY_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=50,
        step="any",
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="NZD/day",
    )
)
FACTOR_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=1.5, step="any", mode=NumberSelectorMode.BOX)
)


def _select(options: list[SelectOptionDict]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _sources_schema(include_settled: bool) -> probatio.Schema:  # noqa: FBT001
    fields: dict[Any, Any] = {}
    if include_settled:
        fields[probatio.Required(CONF_NAME, default="Home")] = TextSelector()
        fields[probatio.Optional(CONF_ICP)] = TextSelector()
        fields[probatio.Required(CONF_SETTLED_ENTITY)] = SENSOR_SELECTOR
    fields[probatio.Optional(CONF_LIVE_ENTITY)] = SENSOR_SELECTOR
    fields[probatio.Optional(CONF_FORECAST_ENTITY)] = SENSOR_SELECTOR
    return probatio.Schema(fields)


def _retailer_schema(*, include_gst: bool, include_date: bool) -> probatio.Schema:
    fields: dict[Any, Any] = {}
    if include_date:
        fields[probatio.Required(CONF_EFFECTIVE_FROM)] = DateSelector()
    fields[probatio.Optional(CONF_PLAN_NAME)] = TextSelector()
    for name in RETAILER_PER_KWH_FIELDS:
        fields[probatio.Required(name, default=0.0)] = CENTS_SELECTOR
    for name in RETAILER_DAILY_FIELDS:
        fields[probatio.Required(name, default=0.0)] = DOLLARS_PER_DAY_SELECTOR
    fields[probatio.Optional(CONF_EXPORT_FLOOR)] = CENTS_SELECTOR
    if include_gst:
        fields[probatio.Required(CONF_GST_INCLUSIVE, default=True)] = BooleanSelector()
    return probatio.Schema(fields)


def _cents_to_nzd(value: float) -> float:
    return round(to_nzd_per_kwh(float(value), "c/kWh"), STORE_DIGITS)


def retailer_version_from_form(
    user_input: Mapping[str, Any], effective_from: str
) -> dict[str, Any]:
    """Convert the retailer form (c/kWh) to a stored version (NZD/kWh)."""
    version: dict[str, Any] = {CONF_EFFECTIVE_FROM: effective_from}
    if user_input.get(CONF_PLAN_NAME):
        version[CONF_PLAN_NAME] = user_input[CONF_PLAN_NAME]
    for name in RETAILER_PER_KWH_FIELDS:
        version[name] = _cents_to_nzd(user_input.get(name, 0.0))
    for name in RETAILER_DAILY_FIELDS:
        version[name] = float(user_input.get(name, 0.0))
    if user_input.get(CONF_EXPORT_FLOOR) is not None:
        version[CONF_EXPORT_FLOOR] = _cents_to_nzd(user_input[CONF_EXPORT_FLOOR])
    return version


def retailer_form_from_version(version: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a stored retailer version back to form values (c/kWh)."""
    values: dict[str, Any] = {}
    if version.get(CONF_PLAN_NAME):
        values[CONF_PLAN_NAME] = version[CONF_PLAN_NAME]
    for name in RETAILER_PER_KWH_FIELDS:
        values[name] = round(version.get(name, 0.0) * 100, 6)
    for name in RETAILER_DAILY_FIELDS:
        values[name] = version.get(name, 0.0)
    if version.get(CONF_EXPORT_FLOOR) is not None:
        values[CONF_EXPORT_FLOOR] = round(version[CONF_EXPORT_FLOOR] * 100, 6)
    return values


def _validate_price_entity(
    flow: ConfigFlow | OptionsFlowWithReload, entity_id: str | None
) -> str | None:
    """Return an error key if the entity isn't a usable price sensor."""
    if not entity_id:
        return None
    state = flow.hass.states.get(entity_id)
    if state is None:
        return "entity_not_found"
    try:
        to_nzd_per_kwh(0.0, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
    except ValueError:
        return "invalid_unit"
    return None


def _validate_sources(
    flow: ConfigFlow | OptionsFlowWithReload,
    user_input: Mapping[str, Any],
    settled: str | None = None,
) -> dict[str, str]:
    """
    Check each source on the form is a price sensor, and that none repeat.

    ``settled`` is the stored settled source when it isn't on the form: it
    only takes part in the duplicate check, so errors land on fields shown.
    """
    errors = {}
    seen: set[str] = {settled} if settled else set()
    for key in SOURCE_KEYS:
        entity_id = user_input.get(key)
        if not entity_id:
            continue
        if entity_id in seen:
            errors[key] = "duplicate_source"
        elif error := _validate_price_entity(flow, entity_id):
            errors[key] = error
        seen.add(entity_id)
    return errors


class ElectricityBillingNzConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for electricity_billing_nz."""

    VERSION = 1
    # 1.2: the optional ICP, not the settled sensor, is the unique id.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialise the flow's collected answers."""
        self._data: dict[str, Any] = {}
        self._title = ""
        self._networks: dict[str, Network] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:  # noqa: ARG004
        """Return the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the ha-electricityinfo-nz price entities."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self, user_input)
            if not str(user_input.get(CONF_NAME, "")).strip():
                errors[CONF_NAME] = "name_required"
            # A field error, not an abort, so the user can clear the ICP and
            # add a comparison entry without re-entering everything.
            icp, icp_error = async_validate_icp(self.hass, user_input.get(CONF_ICP))
            if icp_error:
                errors[CONF_ICP] = icp_error
            if not errors:
                if icp:
                    # One entry per connection; without an ICP any number
                    # of entries can share a node, e.g. to compare plans.
                    await self.async_set_unique_id(icp)
                    self._data[CONF_ICP] = icp
                self._title = user_input[CONF_NAME].strip()
                self._data.update(
                    {
                        k: source_id(self.hass, v)
                        for k, v in user_input.items()
                        if k in SOURCE_KEYS and v
                    }
                )
                return await self.async_step_network()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _sources_schema(include_settled=True), user_input
            ),
            errors=errors,
        )

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the lines company."""
        self._networks = await async_get_networks(self.hass)
        if user_input is not None:
            self._data[CONF_DISTRIBUTOR] = user_input[CONF_DISTRIBUTOR]
            return await self.async_step_category()

        options = [
            SelectOptionDict(value=key, label=network.name)
            for key, network in sorted(self._networks.items())
        ]
        return self.async_show_form(
            step_id="network",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_DISTRIBUTOR): _select(options)}
            ),
        )

    async def async_step_category(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the price and loss category from the ICP's registry record."""
        network = self._networks[self._data[CONF_DISTRIBUTOR]]
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_retailer()

        def codes(values: Mapping[str, Any]) -> list[SelectOptionDict]:
            return [SelectOptionDict(value=c, label=c) for c in sorted(values)]

        return self.async_show_form(
            step_id="category",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_PRICE_CATEGORY): _select(
                        codes(network.categories)
                    ),
                    probatio.Required(CONF_LOSS_CATEGORY): _select(
                        codes(network.loss_categories)
                    ),
                }
            ),
            description_placeholders={"network": network.name},
        )

    async def async_step_retailer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the retailer plan, which applies to all past periods too."""
        if user_input is not None:
            if self.unique_id:
                # Belt and braces: finishing a flow whose unique id an entry
                # already has would make HA replace that entry.
                self._abort_if_unique_id_configured()
            self._data[CONF_GST_INCLUSIVE] = user_input[CONF_GST_INCLUSIVE]
            return self.async_create_entry(
                title=self._title,
                data=self._data,
                options={
                    CONF_RETAILER_VERSIONS: [
                        retailer_version_from_form(user_input, FIRST_EFFECTIVE_FROM)
                    ],
                    CONF_NETWORK_OVERRIDES: [],
                },
            )

        return self.async_show_form(
            step_id="retailer",
            data_schema=_retailer_schema(include_gst=True, include_date=False),
        )


class OptionsFlowHandler(OptionsFlowWithReload):
    """Add rate versions and change source entities."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> ConfigFlowResult:
        """Show the menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_retailer_version",
                "add_network_override",
                "sources",
                "connection",
            ],
        )

    def _latest_retailer(self) -> Mapping[str, Any]:
        versions = self.config_entry.options.get(CONF_RETAILER_VERSIONS, [])
        return max(versions, key=lambda v: v[CONF_EFFECTIVE_FROM], default={})

    async def async_step_add_retailer_version(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add retailer charges that apply from a date."""
        latest = self._latest_retailer()
        errors: dict[str, str] = {}
        if user_input is not None:
            effective_from = str(user_input[CONF_EFFECTIVE_FROM])
            if latest and date.fromisoformat(effective_from) <= date.fromisoformat(
                latest[CONF_EFFECTIVE_FROM]
            ):
                errors[CONF_EFFECTIVE_FROM] = "not_after_latest"
            else:
                versions = [
                    *self.config_entry.options.get(CONF_RETAILER_VERSIONS, []),
                    retailer_version_from_form(user_input, effective_from),
                ]
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_RETAILER_VERSIONS: versions}
                )

        return self.async_show_form(
            step_id="add_retailer_version",
            data_schema=self.add_suggested_values_to_schema(
                _retailer_schema(include_gst=False, include_date=True),
                user_input or retailer_form_from_version(latest),
            ),
            errors=errors,
            description_placeholders={
                "latest": latest.get(CONF_EFFECTIVE_FROM, FIRST_EFFECTIVE_FROM)
            },
        )

    async def async_step_add_network_override(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Override bundled network values from a date."""
        # Read the bundled data, not runtime_data: the entry may not be loaded.
        found = network_for_entry(
            await async_get_networks(self.hass), self.config_entry.data
        )
        if found is None:
            return self.async_abort(reason="unknown_network")
        versions, losses = found.versions, found.losses
        bands = all_bands(versions)

        errors: dict[str, str] = {}
        placeholders = {"first": versions[0].effective_from.isoformat()}
        if user_input is not None:
            override: dict[str, Any] = {
                CONF_EFFECTIVE_FROM: str(user_input[CONF_EFFECTIVE_FROM])
            }
            for name, value in user_input.items():
                if name == CONF_EFFECTIVE_FROM or value is None:
                    continue
                if name.startswith(
                    (OVERRIDE_IMPORT_PREFIX, OVERRIDE_EXPORT_CREDIT_PREFIX)
                ):
                    override[name] = _cents_to_nzd(value)
                else:
                    override[name] = float(value)
            # Overrides before the bundled data have nothing to apply to; a
            # loss-only override only needs the loss data.
            starts = (
                [losses[0].effective_from] if OVERRIDE_LOSS_FACTOR in override else []
            )
            if any(
                k not in (CONF_EFFECTIVE_FROM, OVERRIDE_LOSS_FACTOR) for k in override
            ):
                starts.append(versions[0].effective_from)
            first = max(starts, default=versions[0].effective_from)
            placeholders["first"] = first.isoformat()
            if len(override) == 1:
                errors["base"] = "no_changes"
            elif date.fromisoformat(override[CONF_EFFECTIVE_FROM]) < first:
                errors[CONF_EFFECTIVE_FROM] = "before_network_data"
            else:
                overrides = [
                    *self.config_entry.options.get(CONF_NETWORK_OVERRIDES, []),
                    override,
                ]
                return self.async_create_entry(
                    data={
                        **self.config_entry.options,
                        CONF_NETWORK_OVERRIDES: overrides,
                    }
                )

        fields: dict[Any, Any] = {
            probatio.Required(CONF_EFFECTIVE_FROM): DateSelector(),
            probatio.Optional(OVERRIDE_DAILY): DOLLARS_PER_DAY_SELECTOR,
        }
        for band in bands:
            fields[probatio.Optional(f"{OVERRIDE_IMPORT_PREFIX}{band}")] = (
                CENTS_SELECTOR
            )
        for band in bands:
            fields[probatio.Optional(f"{OVERRIDE_EXPORT_CREDIT_PREFIX}{band}")] = (
                CENTS_SELECTOR
            )
        fields[probatio.Optional(OVERRIDE_LOSS_FACTOR)] = FACTOR_SELECTOR
        return self.async_show_form(
            step_id="add_network_override",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(fields), user_input
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set, change or clear the entry's ICP number."""
        errors: dict[str, str] = {}
        if user_input is not None:
            icp, error = async_validate_icp(
                self.hass, user_input.get(CONF_ICP), self.config_entry
            )
            if error:
                errors[CONF_ICP] = error
            else:
                data = {
                    k: v for k, v in self.config_entry.data.items() if k != CONF_ICP
                } | ({CONF_ICP: icp} if icp else {})
                if icp:
                    # Two entries mustn't share a unique id, ignored ones too.
                    await async_remove_ignored(self.hass, icp)
                # Not used for pricing, so no reload is needed.
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data, unique_id=icp
                )
                return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema({probatio.Optional(CONF_ICP): TextSelector()}),
                user_input
                or (
                    {CONF_ICP: icp}
                    if (icp := self.config_entry.data.get(CONF_ICP))
                    else {}
                ),
            ),
            errors=errors,
        )

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the live and forecast source entities."""
        # The form shows entity ids; the entry stores registry ids.
        current = {
            k: resolve_source(self.hass, v)
            for k, v in self.config_entry.data.items()
            if k in SOURCE_KEYS and v
        }
        if CONF_SETTLED_ENTITY not in current:
            return self.async_abort(reason="unknown_network")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_sources(self, user_input, current[CONF_SETTLED_ENTITY])
            if not errors:
                data = {
                    k: v
                    for k, v in self.config_entry.data.items()
                    if k not in (CONF_LIVE_ENTITY, CONF_FORECAST_ENTITY)
                }
                data.update(
                    {k: source_id(self.hass, v) for k, v in user_input.items() if v}
                )
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
                # Options are unchanged, so the automatic reload won't fire.
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="sources",
            data_schema=self.add_suggested_values_to_schema(
                _sources_schema(include_settled=False),
                user_input or current,
            ),
            errors=errors,
        )
