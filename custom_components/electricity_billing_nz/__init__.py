"""The Electricity Billing NZ integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .engine import BillingEngine, build_tariff
from .networks import async_get_networks, network_for_entry
from .services import async_setup_services
from .sources import async_store_registry_ids

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .networks import Network

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class RuntimeData:
    """Runtime data for a Electricity Billing NZ config entry."""

    network: Network
    engine: BillingEngine


type ElectricityBillingNzConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: ARG001
    """Register the integration's services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricityBillingNzConfigEntry,
) -> bool:
    """Set up Electricity Billing NZ from a config entry."""
    async_store_registry_ids(hass, entry)
    found = network_for_entry(await async_get_networks(hass), entry.data)
    if found is None:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="unknown_network"
        )
    network = found.network

    engine = BillingEngine(
        hass, entry, build_tariff(network, entry.data, entry.options)
    )
    entry.runtime_data = RuntimeData(network=network, engine=engine)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start after the entities exist so their first update isn't missed.
    engine.async_start()
    entry.async_on_unload(engine.async_stop)

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ElectricityBillingNzConfigEntry,
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
