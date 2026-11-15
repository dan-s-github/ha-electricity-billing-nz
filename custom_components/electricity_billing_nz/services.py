"""Services for Electricity Billing NZ."""

from __future__ import annotations

import math
from dataclasses import asdict, fields
from typing import TYPE_CHECKING, Any

import probatio
import yaml
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_SPOT,
    ATTR_SPOT_UNIT,
    ATTR_TIMESTAMP,
    DOMAIN,
    PRICE_PRECISION,
    PRICE_UNIT,
    SERVICE_EXPORT_VERSIONS,
    SERVICE_PRICE_AT,
)
from .engine import parse_time
from .tariff import NoRatesError, period_start, to_nzd_per_kwh

if TYPE_CHECKING:
    from . import ElectricityBillingNzConfigEntry

PRICE_AT_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Required(ATTR_TIMESTAMP): cv.datetime,
        probatio.Required(ATTR_SPOT): probatio.Coerce(float),
        probatio.Optional(ATTR_SPOT_UNIT, default=PRICE_UNIT): cv.string,
    }
)
EXPORT_VERSIONS_SCHEMA = probatio.Schema(
    {probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string}
)


def _entry(call: ServiceCall) -> ElectricityBillingNzConfigEntry:
    return service.async_get_config_entry(
        call.hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY_ID]
    )


async def _async_price_at(call: ServiceCall) -> ServiceResponse:
    """Price a spot value at a past or future time, with the rates in force then."""
    engine = _entry(call).runtime_data.engine
    if not math.isfinite(call.data[ATTR_SPOT]):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_spot"
        )
    try:
        spot = to_nzd_per_kwh(call.data[ATTR_SPOT], call.data[ATTR_SPOT_UNIT])
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_unit",
            translation_placeholders={"unit": call.data[ATTR_SPOT_UNIT]},
        ) from err
    # A timestamp without an offset is in HA's time zone, as for the sources.
    timestamp = parse_time(call.data[ATTR_TIMESTAMP])
    if timestamp is None:  # cv.datetime already validated it
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_timestamp"
        )
    try:
        pair = engine.price_pair(spot, period_start(timestamp))
    except NoRatesError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_rates",
            translation_placeholders={
                "period_start": err.at.isoformat(),
                "missing": ", ".join(err.missing),
            },
        ) from err
    return {
        "period_start": pair.period_start.isoformat(),
        "import_price": round(pair.import_price.price, PRICE_PRECISION),
        "export_price": round(pair.export_price.price, PRICE_PRECISION),
        "import": pair.import_price.as_attributes(),
        "export": pair.export_price.as_attributes(),
    }


def _plain(value: Any) -> Any:
    """Make dataclass dicts YAML friendly (dates as ISO strings)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, frozenset)):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def _async_export_versions(call: ServiceCall) -> ServiceResponse:
    """Return every rate version in force for the entry, as YAML."""
    entry = _entry(call)
    tariff = entry.runtime_data.engine.tariff
    document = {
        "entry": entry.title,
        "network": [
            {f.name: getattr(version, f.name) for f in fields(version)}
            | {"bands": version.bands.name}
            for version in tariff.network
        ],
        "loss": [asdict(version) for version in tariff.loss],
        "retailer": [asdict(version) for version in tariff.retailer],
        "gst": tariff.gst,
    }
    return {"yaml": yaml.safe_dump(_plain(document), sort_keys=False)}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_PRICE_AT,
        _async_price_at,
        schema=PRICE_AT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_VERSIONS,
        _async_export_versions,
        schema=EXPORT_VERSIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
