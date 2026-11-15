"""Test the Electricity Billing NZ config and options flows."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electricity_billing_nz.const import DOMAIN

from .conftest import ECOTRICITY, FORECAST, LIVE, SETTLED, entry_data

RETAILER_FORM = {
    "plan_name": "ecoWHOLESALE",
    "import_margin": 2.86,
    "export_margin": 1.90,
    "transmission": 3.02,
    "levy": 0.20,
    "metering_daily": 0.35,
    "retailer_daily": 0.15,
    "gst_inclusive": True,
}


@pytest.fixture(autouse=True)
def sources(hass: HomeAssistant) -> None:
    """Provide the ha-electricityinfo-nz entities the flow validates."""
    for entity_id in (SETTLED, LIVE, FORECAST):
        hass.states.async_set(entity_id, "12.0", {"unit_of_measurement": "c/kWh"})


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_full_flow(hass: HomeAssistant) -> None:
    """Sources, network, categories and retailer plan create an entry."""
    result = await _start(hass)
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Home",
            "settled_entity": SETTLED,
            "live_entity": LIVE,
            "forecast_entity": FORECAST,
        },
    )
    assert result["step_id"] == "network"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"distributor": "vector"}
    )
    assert result["step_id"] == "category"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"price_category": "ARHLC", "loss_category": "VECA1"}
    )
    assert result["step_id"] == "retailer"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], RETAILER_FORM
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["result"].unique_id == SETTLED
    assert result["data"] == {
        "settled_entity": SETTLED,
        "live_entity": LIVE,
        "forecast_entity": FORECAST,
        "distributor": "vector",
        "price_category": "ARHLC",
        "loss_category": "VECA1",
        "gst_inclusive": True,
    }
    assert result["options"] == {
        "retailer_versions": [
            {
                "effective_from": "2000-01-01",
                "plan_name": "ecoWHOLESALE",
                "import_margin": 0.0286,
                "export_margin": 0.019,
                "transmission": 0.0302,
                "levy": 0.002,
                "metering_daily": 0.35,
                "retailer_daily": 0.15,
            }
        ],
        "network_overrides": [],
    }


@pytest.mark.parametrize(
    ("unit", "error"), [("kWh", "invalid_unit"), (None, "invalid_unit")]
)
async def test_invalid_source_unit(
    hass: HomeAssistant, unit: str | None, error: str
) -> None:
    """The settled sensor must carry a price unit."""
    hass.states.async_set(SETTLED, "12.0", {"unit_of_measurement": unit})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Home", "settled_entity": SETTLED}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"settled_entity": error}


async def test_missing_source(hass: HomeAssistant) -> None:
    """A source entity that doesn't exist is reported."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Home", "settled_entity": "sensor.nope"}
    )
    assert result["errors"] == {"settled_entity": "entity_not_found"}


@pytest.mark.usefixtures("config_entry")
async def test_already_configured(hass: HomeAssistant) -> None:
    """One entry per settled price sensor."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Again", "settled_entity": SETTLED}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def _options(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> dict:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_add_retailer_version(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A new retailer version is appended, prefilled from the latest."""
    result = await _options(hass, config_entry, "add_retailer_version")
    assert result["step_id"] == "add_retailer_version"

    form = {k: v for k, v in RETAILER_FORM.items() if k != "gst_inclusive"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**form, "effective_from": "2027-04-01", "import_margin": 3.1},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    versions = config_entry.options["retailer_versions"]
    assert [v["effective_from"] for v in versions] == ["2000-01-01", "2027-04-01"]
    assert versions[-1]["import_margin"] == 0.031


async def test_retailer_version_must_be_newer(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Versions are only added after the latest one."""
    result = await _options(hass, config_entry, "add_retailer_version")
    form = {k: v for k, v in RETAILER_FORM.items() if k != "gst_inclusive"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**form, "effective_from": "1999-01-01"}
    )
    assert result["errors"] == {"effective_from": "not_after_latest"}


async def test_add_network_override(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Only the values entered are stored, converted to NZD."""
    result = await _options(hass, config_entry, "add_network_override")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"effective_from": "2026-10-01", "import_peak": 15.13}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["network_overrides"] == [
        {"effective_from": "2026-10-01", "import_peak": 0.1513}
    ]
    tariff = config_entry.runtime_data.engine.tariff
    assert tariff.network[-1].import_rates["peak"] == 0.1513


async def test_network_override_needs_a_value(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A date alone is not an override."""
    result = await _options(hass, config_entry, "add_network_override")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"effective_from": "2026-10-01"}
    )
    assert result["errors"] == {"base": "no_changes"}


async def test_network_override_before_bundled_data(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """An override dated before the bundled schedule would never apply."""
    result = await _options(hass, config_entry, "add_network_override")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"effective_from": "2025-01-01", "import_peak": 15.0}
    )
    assert result["errors"] == {"effective_from": "before_network_data"}
    assert result["description_placeholders"] == {"first": "2026-04-01"}


async def test_network_override_when_not_loaded(hass: HomeAssistant) -> None:
    """The override form doesn't need the entry to be loaded."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(),
        options={"retailer_versions": [ECOTRICITY], "network_overrides": []},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_network_override"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_network_override"


async def test_network_override_unknown_category(hass: HomeAssistant) -> None:
    """A category missing from the bundled data aborts instead of crashing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data=entry_data(price_category="NOPE"),
        options={"retailer_versions": [ECOTRICITY], "network_overrides": []},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_network_override"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_network"


async def test_change_sources(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Removing the live source drops it from the entry and reloads."""
    result = await _options(hass, config_entry, "sources")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"forecast_entity": FORECAST}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "live_entity" not in config_entry.data
    assert config_entry.runtime_data.engine.live_entity is None
    assert hass.states.get("sensor.home_import_price_live") is None
    assert hass.states.get("sensor.home_import_price_forecast") is not None


async def test_name_required(hass: HomeAssistant) -> None:
    """A blank name is rejected."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "   ", "settled_entity": SETTLED}
    )
    assert result["errors"] == {"name": "name_required"}


async def test_duplicate_source(hass: HomeAssistant) -> None:
    """Each source must be a different sensor."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Home", "settled_entity": SETTLED, "live_entity": SETTLED},
    )
    assert result["errors"] == {"live_entity": "duplicate_source"}


async def test_options_source_same_as_settled(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The live source can't be the settled sensor."""
    result = await _options(hass, config_entry, "sources")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"live_entity": SETTLED}
    )
    assert result["errors"] == {"live_entity": "duplicate_source"}


async def test_network_override_scaffold_entry(hass: HomeAssistant) -> None:
    """An entry without network fields aborts instead of crashing."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", data={"name": "Home"}, options={}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_network_override"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_network"


async def test_options_sources_ignore_missing_settled(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A settled sensor with no state doesn't block changing the other sources."""
    result = await _options(hass, config_entry, "sources")
    hass.states.async_remove(SETTLED)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"live_entity": LIVE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_sources_scaffold_entry(hass: HomeAssistant) -> None:
    """An entry without sources aborts instead of crashing."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={"name": "Home"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sources"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_network"
