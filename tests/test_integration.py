"""Test integration setup and unload for Electricity Billing NZ."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.electricity_billing_nz.const import DOMAIN

from .conftest import entry_data


async def test_setup_and_unload_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test a config entry sets up, stores runtime data and unloads."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.network.key == "vector"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED


# The entry stays loaded, so its half-hour timer is still scheduled.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_unload_entry_platform_failure(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test the entry stays loaded when a platform fails to unload."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=False),
    ):
        assert not await hass.config_entries.async_unload(config_entry.entry_id)

    assert config_entry.state is ConfigEntryState.FAILED_UNLOAD


async def test_unknown_category_fails_setup(hass: HomeAssistant) -> None:
    """A category no longer in the bundled data is a setup error."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", data=entry_data(price_category="NOPE")
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_entry_without_network_fields_fails_setup(hass: HomeAssistant) -> None:
    """An entry from the name-only scaffold flow is a setup error, not a crash."""
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={"name": "Home"})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
