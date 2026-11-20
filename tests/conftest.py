"""Pytest fixtures for custom integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

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

if TYPE_CHECKING:
    from collections.abc import Iterator

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


# A frozen copy of the bundled network data the tests are written against,
# so bundling a new schedule (see tools/vector_schedule.py) doesn't change
# which version is latest. test_networks.py checks the live data.
NETWORK_SNAPSHOT = Path(__file__).parent / "fixtures" / "networks"


@pytest.fixture(autouse=True)
def bundled_network_snapshot() -> Iterator[None]:
    """Point the integration's network directory at the frozen data."""
    with patch(
        "custom_components.electricity_billing_nz.networks.NETWORKS_DIR",
        NETWORK_SNAPSHOT,
    ):
        yield


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
