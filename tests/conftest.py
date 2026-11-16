"""Pytest fixtures for custom integration tests."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electricity_billing_nz.const import (
    CONF_DISTRIBUTOR,
    CONF_FORECAST_ENTITY,
    CONF_GST_INCLUSIVE,
    CONF_LIVE_ENTITY,
    CONF_LOSS_CATEGORY,
    CONF_NETWORK_OVERRIDES,
    CONF_PRICE_CATEGORY,
    CONF_RETAILER_VERSIONS,
    CONF_SETTLED_ENTITY,
    DOMAIN,
)

SETTLED = "sensor.ros0221_settled_price"
ICP = "0000012345VE123"
LIVE = "sensor.ros0221_live_price"
FORECAST = "sensor.ros0221_day_ahead_forecast"

# The Ecotricity ecoWHOLESALE plan the spec reconciles against, ex GST, NZD.
ECOTRICITY = {
    "effective_from": "2000-01-01",
    "plan_name": "ecoWHOLESALE",
    "import_margin": 0.0286,
    "export_margin": 0.019,
    "transmission": 0.0302,
    "levy": 0.002,
    "metering_daily": 0.35,
    "retailer_daily": 0.15,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading integrations from custom_components in tests."""


@pytest.fixture(autouse=True)
async def nz_time_zone(hass: HomeAssistant) -> None:
    """Run every test in New Zealand time."""
    await hass.config.async_set_time_zone("Pacific/Auckland")


def entry_data(**overrides: Any) -> dict[str, Any]:
    """Return config entry data for ARHLC on Vector."""
    return {
        CONF_SETTLED_ENTITY: SETTLED,
        CONF_LIVE_ENTITY: LIVE,
        CONF_FORECAST_ENTITY: FORECAST,
        CONF_DISTRIBUTOR: "vector",
        CONF_PRICE_CATEGORY: "ARHLC",
        CONF_LOSS_CATEGORY: "VECA1",
        CONF_GST_INCLUSIVE: True,
        **overrides,
    }


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a Vector ARHLC entry with the Ecotricity plan, added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(),
        options={CONF_RETAILER_VERSIONS: [ECOTRICITY], CONF_NETWORK_OVERRIDES: []},
    )
    entry.add_to_hass(hass)
    return entry
