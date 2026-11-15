"""Base entity for Electricity Billing NZ."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .engine import signal_updated

if TYPE_CHECKING:
    from . import ElectricityBillingNzConfigEntry
    from .engine import BillingEngine


class BillingEntity(Entity):
    """An entity fed by the entry's billing engine."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ElectricityBillingNzConfigEntry, key: str) -> None:
        """Initialise the entity for one config entry."""
        self.engine: BillingEngine = entry.runtime_data.engine
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Electricity Billing NZ",
            model=f"{entry.runtime_data.network.name} tariff",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Follow engine updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_updated(self.engine.entry.entry_id),
                self._handle_engine_update,
            )
        )

    @callback
    def _handle_engine_update(self) -> None:
        self.async_write_ha_state()


class TariffEntity(BillingEntity):
    """An entity describing the tariff now: available while every rate is in force."""

    @property
    def available(self) -> bool:
        """Return False unless every rate is in force now (see BillingEngine)."""
        return self.engine.rates is not None
