"""Support for Electricity Billing NZ sensors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers import entity_registry as er

from .const import DAILY_UNIT, DOMAIN, PRICE_PRECISION, PRICE_UNIT
from .entity import BillingEntity, TariffEntity
from .tariff import PriceBreakdown, all_bands

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import ElectricityBillingNzConfigEntry
    from .engine import PricePair

type Source = Literal["settled", "live"]
type Side = Literal["import", "export"]

# Restored attributes: the price breakdown only, not HA's own (unit, name...).
RESTORED_ATTRIBUTES = PriceBreakdown.attribute_keys() | {
    "period_start",
    "trading_period",
}
OPTIONAL_KEYS = (
    "import_price_live",
    "export_price_live",
    "import_price_forecast",
    "export_price_forecast",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElectricityBillingNzConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the price, band and daily charge sensors."""
    engine = entry.runtime_data.engine
    entities: list[SensorEntity] = [
        PriceSensor(entry, "import_price", "settled", "import"),
        PriceSensor(entry, "export_price", "settled", "export"),
        DailyChargeSensor(entry, "daily_charge"),
        TouBandSensor(entry, "tou_band"),
        NextBandChangeSensor(entry, "next_band_change"),
    ]
    if engine.live_entity:
        entities += [
            PriceSensor(entry, "import_price_live", "live", "import"),
            PriceSensor(entry, "export_price_live", "live", "export"),
        ]
    if engine.forecast_entity:
        entities += [
            ForecastPriceSensor(entry, "import_price_forecast", "import"),
            ForecastPriceSensor(entry, "export_price_forecast", "export"),
        ]
    async_add_entities(entities)

    # Drop sensors whose optional source was removed in the options.
    registry = er.async_get(hass)
    created = {entity.unique_id for entity in entities}
    for key in OPTIONAL_KEYS:
        unique_id = f"{entry.entry_id}_{key}"
        if unique_id not in created and (
            entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        ):
            registry.async_remove(entity_id)


class PriceSensor(BillingEntity, RestoreSensor):
    """Landed import or export price for the settled or live period."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        entry: ElectricityBillingNzConfigEntry,
        key: str,
        source: Source,
        side: Side,
    ) -> None:
        """Initialise the price sensor."""
        super().__init__(entry, key)
        self._source = source
        self._side = side
        self._restored_value: float | None = None
        self._restored_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the last price until the source reports."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None and isinstance(
            data.native_value, (int, float)
        ):
            self._restored_value = float(data.native_value)
            if (state := await self.async_get_last_state()) is not None:
                self._restored_attributes = {
                    k: v
                    for k, v in state.attributes.items()
                    if k in RESTORED_ATTRIBUTES
                }

    @property
    def _pair(self) -> PricePair | None:
        return self.engine.settled if self._source == "settled" else self.engine.live

    @property
    def _source_entity(self) -> str | None:
        return (
            self.engine.settled_entity
            if self._source == "settled"
            else self.engine.live_entity
        )

    @property
    def _use_restored(self) -> bool:
        return (
            self._pair is None
            and self._restored_value is not None
            and self._source_entity not in self.engine.source_reported
        )

    @property
    def available(self) -> bool:
        """Return False when the spot price is unavailable."""
        return self._pair is not None or self._use_restored

    @property
    def native_value(self) -> float | None:
        """Return the landed price in NZD/kWh."""
        if (pair := self._pair) is not None:
            breakdown = (
                pair.import_price if self._side == "import" else pair.export_price
            )
            return round(breakdown.price, PRICE_PRECISION)
        return self._restored_value if self._use_restored else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the price components (ex GST) and the period priced."""
        if (pair := self._pair) is None:
            return self._restored_attributes if self._use_restored else None
        breakdown = pair.import_price if self._side == "import" else pair.export_price
        return {
            **breakdown.as_attributes(),
            "period_start": pair.period_start.isoformat(),
            "trading_period": pair.trading_period,
        }


class ForecastPriceSensor(BillingEntity, SensorEntity):
    """Current or next forecast price, with every forecast period as a list."""

    _attr_native_unit_of_measurement = PRICE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    # The list changes every update and would bloat the recorder.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self, entry: ElectricityBillingNzConfigEntry, key: str, side: Side
    ) -> None:
        """Initialise the forecast sensor."""
        super().__init__(entry, key)
        self._side = side

    def _price(self, row: Any) -> float:
        value = row.import_price if self._side == "import" else row.export_price
        return round(value, PRICE_PRECISION)

    @property
    def available(self) -> bool:
        """
        Return False when no forecast period can be priced.

        With priced later periods but none for now, it stays available with
        an unknown state, so the forecast list can still be read.
        """
        return self.engine.forecast_available

    @property
    def native_value(self) -> float | None:
        """Return the price for the current period (or the next, if not listed)."""
        row = self.engine.forecast_current
        return self._price(row) if row is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return one row per trading period."""
        return {
            "forecast": [
                {
                    "period_start": row.period_start.isoformat(),
                    "trading_period": row.trading_period,
                    "price": self._price(row),
                    "tou_band": row.tou_band,
                }
                for row in self.engine.forecast
            ]
        }


class DailyChargeSensor(TariffEntity, SensorEntity):
    """Fixed daily charges: network, metering and retailer."""

    _attr_native_unit_of_measurement = DAILY_UNIT
    _attr_suggested_display_precision = 4

    @property
    def native_value(self) -> float | None:
        """Return the daily charge in NZD."""
        value = self.engine.daily_charge
        return round(value, PRICE_PRECISION) if value is not None else None


class TouBandSensor(TariffEntity, SensorEntity):
    """Current time-of-use band."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, entry: ElectricityBillingNzConfigEntry, key: str) -> None:
        """Initialise with every band the tariff can resolve to."""
        super().__init__(entry, key)
        self._attr_options = all_bands(self.engine.tariff.network)

    @property
    def native_value(self) -> str | None:
        """Return the band name."""
        return self.engine.tou_band


class NextBandChangeSensor(TariffEntity, SensorEntity):
    """When the time-of-use band next changes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Return the next band change."""
        return self.engine.next_band_change
