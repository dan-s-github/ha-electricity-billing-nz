"""Test the Electricity Billing NZ services."""

from __future__ import annotations

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electricity_billing_nz.const import DOMAIN

from .helpers import template_export, template_import


@pytest.fixture
async def loaded(hass: HomeAssistant, config_entry: MockConfigEntry) -> MockConfigEntry:
    """Return the entry, set up."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


@pytest.mark.parametrize(
    ("timestamp", "spot", "unit", "peak"),
    [
        ("2026-07-15T17:12:00+12:00", 0.12, "NZD/kWh", True),
        ("2026-07-15T16:59:00+12:00", 12.0, "c/kWh", False),
        ("2026-11-16T18:00:00", 120.0, "$/MWh", False),
    ],
)
async def test_price_at(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    *,
    timestamp: str,
    spot: float,
    unit: str,
    peak: bool,
) -> None:
    """price_at prices any time with the rates in force then."""
    response = await hass.services.async_call(
        DOMAIN,
        "price_at",
        {
            "config_entry_id": loaded.entry_id,
            "timestamp": timestamp,
            "spot": spot,
            "spot_unit": unit,
        },
        blocking=True,
        return_response=True,
    )
    assert response["import_price"] == pytest.approx(
        template_import(0.12, peak=peak), abs=1e-6
    )
    assert response["export_price"] == pytest.approx(
        template_export(0.12, peak=peak), abs=1e-6
    )
    assert response["import"]["tou_band"] == ("peak" if peak else "offpeak")


async def test_price_at_bad_unit(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """An unknown spot unit is rejected."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "price_at",
            {
                "config_entry_id": loaded.entry_id,
                "timestamp": "2026-07-15T17:00:00+12:00",
                "spot": 1,
                "spot_unit": "kWh",
            },
            blocking=True,
            return_response=True,
        )


@pytest.mark.parametrize("spot", ["nan", "inf"])
async def test_price_at_non_finite_spot(
    hass: HomeAssistant, loaded: MockConfigEntry, spot: str
) -> None:
    """NaN and infinity aren't spot prices."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "price_at",
            {
                "config_entry_id": loaded.entry_id,
                "timestamp": "2026-07-15T17:00:00+12:00",
                "spot": spot,
            },
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "invalid_spot"


async def test_price_at_before_rate_data(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    """Before the bundled data starts there's an error, not a too-low price."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "price_at",
            {
                "config_entry_id": loaded.entry_id,
                "timestamp": "2026-03-15T17:00:00+13:00",
                "spot": 0.12,
            },
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "no_rates"


async def test_export_versions(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """export_versions returns the merged versions as YAML."""
    response = await hass.services.async_call(
        DOMAIN,
        "export_versions",
        {"config_entry_id": loaded.entry_id},
        blocking=True,
        return_response=True,
    )
    document = yaml.safe_load(response["yaml"])
    assert document["network"][0]["effective_from"] == "2026-04-01"
    assert document["network"][0]["bands"] == "vector_tou_2026"
    assert document["loss"][0]["factor"] == 1.0476
    assert document["retailer"][0]["import_margin"] == 0.0286
