"""Test the Electricity Billing NZ config and options flows."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electricity_billing_nz.const import DOMAIN
from custom_components.electricity_billing_nz.icp import normalise_icp

from .conftest import ECOTRICITY, FORECAST, ICP, LIVE, SETTLED, entry_data

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
    # No ICP entered: no unique id, so more entries can share the node.
    assert result["result"].unique_id is None
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


async def test_icp_is_unique_id(hass: HomeAssistant) -> None:
    """An ICP is normalised, stored and used as the unique id."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Home", "icp": " 0000012345ve123 ", "settled_entity": SETTLED},
    )
    assert result["step_id"] == "network"
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["unique_id"] == ICP


async def test_already_configured(hass: HomeAssistant) -> None:
    """One entry per ICP: a used one is a field error, so it can be cleared."""
    MockConfigEntry(
        domain=DOMAIN, title="Home", unique_id=ICP, data=entry_data(icp=ICP)
    ).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Again", "icp": ICP, "settled_entity": SETTLED}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"icp": "icp_in_use"}


@pytest.mark.usefixtures("config_entry")
async def test_same_node_without_icp(hass: HomeAssistant) -> None:
    """Without an ICP a second entry can use the same node, e.g. to compare plans."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Compare", "settled_entity": SETTLED}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "network"


@pytest.mark.parametrize(
    "icp",
    # The last uses full-width digits, which mustn't count as 0-9.
    ["12345", "0000012345VE12", "000001234AVE123", "０００００12345VE123"],  # noqa: RUF001
)
async def test_invalid_icp(hass: HomeAssistant, icp: str) -> None:
    """An ICP must be 10 digits, 2 letters and 3 check characters."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Home", "icp": icp, "settled_entity": SETTLED}
    )
    assert result["errors"] == {"icp": "invalid_icp"}


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
    assert result["description_placeholders"] == {"first": "2025-04-01"}


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


@pytest.mark.parametrize("gst_inclusive", [True, False])
async def test_options_gst(
    hass: HomeAssistant, config_entry: MockConfigEntry, *, gst_inclusive: bool
) -> None:
    """The GST choice can be changed; the entry reloads only when it changes."""
    assert config_entry.data["gst_inclusive"] is True
    result = await _options(hass, config_entry, "gst")
    engine = config_entry.runtime_data.engine
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"gst_inclusive": gst_inclusive}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.data["gst_inclusive"] is gst_inclusive
    assert config_entry.runtime_data.engine.tariff.gst == (
        1.15 if gst_inclusive else 1.0
    )
    reloaded = config_entry.runtime_data.engine is not engine
    assert reloaded is not gst_inclusive


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
    assert result["reason"] == "settled_not_found"


async def test_set_and_clear_icp(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The options set the ICP as unique id, and clearing it removes both."""
    result = await _options(hass, config_entry, "connection")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"icp": ICP}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.unique_id == ICP
    # Removed, as a finished config flow would, so no unique id is shared.
    assert [e.title for e in hass.config_entries.async_entries(DOMAIN)] == ["Home"]

    assert config_entry.data["icp"] == ICP

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.unique_id is None
    assert "icp" not in config_entry.data


async def test_icp_in_use(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Another entry's ICP can't be reused."""
    MockConfigEntry(
        domain=DOMAIN,
        title="Flat",
        unique_id=ICP,
        minor_version=2,
        data=entry_data(icp=ICP),
    ).add_to_hass(hass)
    result = await _options(hass, config_entry, "connection")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"icp": ICP}
    )
    assert result["errors"] == {"icp": "icp_in_use"}


async def test_icp_in_use_by_setup_in_progress(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """An ICP held by a setup still in progress can't be given to an entry."""
    result = await _start(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Flat", "icp": ICP, "settled_entity": SETTLED}
    )
    result = await _options(hass, config_entry, "connection")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"icp": ICP}
    )
    assert result["errors"] == {"icp": "icp_in_setup"}


async def test_ignored_entry_does_not_hold_icp(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """An ignored entry doesn't block the ICP, as in the config flow."""
    MockConfigEntry(
        domain=DOMAIN,
        title="Ignored",
        unique_id=ICP,
        source=config_entries.SOURCE_IGNORE,
    ).add_to_hass(hass)
    result = await _options(hass, config_entry, "connection")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"icp": ICP}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.unique_id == ICP


@pytest.mark.parametrize("icp", ["0000012345ve123", "0000012345-VE 123"])
def test_icp_normalised(icp: str) -> None:
    """Case, spaces and dashes don't make a second spelling."""
    assert normalise_icp(icp) == ICP


def test_icp_non_ascii_rejected() -> None:
    """A letter that upper-cases to ASCII ("ﬀ" -> "FF") isn't an ICP."""
    with pytest.raises(ValueError, match="Not an ICP"):
        normalise_icp("0000012345\ufb00123")
