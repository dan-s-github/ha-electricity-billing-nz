"""Test that source entities are followed through renames."""

from __future__ import annotations

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electricity_billing_nz.const import DOMAIN

from .conftest import ECOTRICITY, SETTLED, entry_data
from .helpers import nz, set_settled, template_import

IMPORT = "sensor.home_import_price"
RENAMED = "sensor.node_settled_price"


@pytest.fixture
def settled_registry_id(entity_registry: er.EntityRegistry) -> str:
    """Register the settled source, as ha-electricityinfo-nz does."""
    entry = entity_registry.async_get_or_create(
        "sensor", "electricityinfo", "ros0221_settled", suggested_object_id=SETTLED[7:]
    )
    assert entry.entity_id == SETTLED
    return entry.id


async def test_flow_stores_registry_id(
    hass: HomeAssistant, settled_registry_id: str
) -> None:
    """The flow stores the settled source by registry id."""
    hass.states.async_set(SETTLED, "12.0", {"unit_of_measurement": "c/kWh"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    for step in (
        {"name": "Home", "settled_entity": SETTLED},
        {"distributor": "vector"},
        {"price_category": "ARHLC", "loss_category": "VECA1"},
        {"gst_inclusive": True},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], step)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["settled_entity"] == settled_registry_id


async def test_entity_id_switched_to_registry_id(
    hass: HomeAssistant, settled_registry_id: str
) -> None:
    """
    An old entry is updated at setup.

    The settled source switches to its registry id, and the old unique id
    (the settled sensor) is dropped: one node can feed several entries.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=SETTLED,
        data=entry_data(),
        options={"retailer_versions": [ECOTRICITY], "network_overrides": []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data["settled_entity"] == settled_registry_id
    assert entry.unique_id is None
    # Unregistered sources stay as entity ids.
    assert entry.data["live_entity"] == "sensor.ros0221_live_price"


async def test_renamed_source_is_followed(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    settled_registry_id: str,
) -> None:
    """After a rename the prices come from the new entity id."""
    freezer.move_to(nz("2026-11-16 12:10"))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=settled_registry_id,
        data=entry_data(settled_entity=settled_registry_id),
        options={"retailer_versions": [ECOTRICITY], "network_overrides": []},
    )
    entry.add_to_hass(hass)
    set_settled(hass, 10.0, nz("2026-11-16 11:30"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.engine.settled_entity == SETTLED

    entity_registry.async_update_entity(SETTLED, new_entity_id=RENAMED)
    await hass.async_block_till_done()
    assert entry.runtime_data.engine.settled_entity == RENAMED

    hass.states.async_set(
        RENAMED,
        "20.0",
        {
            "unit_of_measurement": "c/kWh",
            "timestamp": nz("2026-11-16 11:30").isoformat(),
            "trading_period": 24,
        },
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(IMPORT).state) == pytest.approx(
        template_import(0.20, peak=False), abs=1e-6
    )
