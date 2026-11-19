"""The Electricity Billing NZ integration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .billing import STORAGE_VERSION, BillingMeter
from .const import (
    CONF_BILLING_CYCLE_DAY,
    CONF_IMPORT_ENERGY_ENTITY,
    DEFAULT_BILLING_CYCLE_DAY,
    DOMAIN,
)
from .engine import ENERGY_OPTIONS, BillingEngine, build_tariff
from .networks import async_get_networks, network_for_entry
from .services import async_setup_services
from .sources import async_store_registry_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .networks import Network

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
# Seconds to wait before writing the billing meter after a change.
SAVE_DELAY = 30


@dataclass
class RuntimeData:
    """Runtime data for a Electricity Billing NZ config entry."""

    network: Network
    engine: BillingEngine
    # The billing meter's storage, when billing is set up.
    store: Store[dict[str, Any]] | None = None


type ElectricityBillingNzConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: ARG001
    """Register the integration's services."""
    async_setup_services(hass)
    return True


async def async_migrate_entry(
    hass: HomeAssistant, entry: ElectricityBillingNzConfigEntry
) -> bool:
    """Migrate an entry to the current config entry version."""
    if entry.version > 1:
        return False  # from a newer release; can't downgrade
    if entry.minor_version < 2:  # noqa: PLR2004
        # 1.1 used the settled sensor as unique id, but one node sensor can
        # feed several entries. 1.1 entries have no ICP, which is the unique
        # id now, so they have none.
        hass.config_entries.async_update_entry(entry, unique_id=None, minor_version=2)
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

    tariff = build_tariff(network, entry.data, entry.options)
    meter: BillingMeter | None = None
    store: Store[dict[str, Any]] | None = None
    save: Callable[[], None] | None = None
    if entry.options.get(CONF_IMPORT_ENERGY_ENTITY):
        store = _billing_store(hass, entry)
        meter = BillingMeter(
            tariff,
            int(entry.options.get(CONF_BILLING_CYCLE_DAY, DEFAULT_BILLING_CYCLE_DAY)),
            await store.async_load(),
            # Stored registry ids, which stay the same when a meter is renamed.
            {
                side: entry.options[key]
                for side, key in ENERGY_OPTIONS
                if entry.options.get(key)
            },
        )
        save = partial(store.async_delay_save, meter.as_dict, SAVE_DELAY)

    engine = BillingEngine(hass, entry, tariff, meter, save)
    entry.runtime_data = RuntimeData(network=network, engine=engine, store=store)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start after the entities exist so their first update isn't missed.
    engine.async_start()
    entry.async_on_unload(engine.async_stop)

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ElectricityBillingNzConfigEntry,
) -> bool:
    """Unload a config entry, saving the billing meter first."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data
    # Stop first, so no update can schedule a save after this one.
    runtime.engine.async_stop()
    if runtime.store is not None and runtime.engine.meter is not None:
        await runtime.store.async_save(runtime.engine.meter.as_dict())
    return True


async def async_remove_entry(
    hass: HomeAssistant, entry: ElectricityBillingNzConfigEntry
) -> None:
    """Delete the entry's billing meter."""
    await _billing_store(hass, entry).async_remove()


class _BillingStore(Store[dict[str, Any]]):
    """The billing meter's storage; BillingMeter reads version 1 data too."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,  # noqa: ARG002
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise NotImplementedError
        return old_data


def _billing_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    return _BillingStore(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
