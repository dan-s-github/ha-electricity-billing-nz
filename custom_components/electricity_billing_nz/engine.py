"""Turn source spot prices into landed prices and push them to the entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .billing import to_kwh
from .const import (
    CONF_EFFECTIVE_FROM,
    CONF_EXPORT_ENERGY_ENTITY,
    CONF_EXPORT_FLOOR,
    CONF_FORECAST_ENTITY,
    CONF_GST_INCLUSIVE,
    CONF_IMPORT_ENERGY_ENTITY,
    CONF_LIVE_ENTITY,
    CONF_LOSS_CATEGORY,
    CONF_NETWORK_OVERRIDES,
    CONF_PRICE_CATEGORY,
    CONF_RETAILER_VERSIONS,
    CONF_SETTLED_ENTITY,
    DOMAIN,
    LOGGER,
    RETAILER_DAILY_FIELDS,
    RETAILER_PER_KWH_FIELDS,
)
from .networks import apply_loss_overrides, apply_network_overrides
from .sources import resolve_source
from .tariff import (
    GST_RATE,
    NZ_TZ,
    PERIODS_PER_DAY_MAX,
    NoRatesError,
    PriceBreakdown,
    Rates,
    RetailerVersion,
    Tariff,
    add_periods,
    next_tariff_band_change,
    period_start,
    prices,
    rates_at,
    to_nzd_per_kwh,
    trading_period_start,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import (
        EventStateChangedData,
        EventStateReportedData,
        State,
    )
    from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData

    from .billing import Bill, BillingMeter, Side
    from .networks import Network

RETAILER_FIELDS = (*RETAILER_PER_KWH_FIELDS, *RETAILER_DAILY_FIELDS, CONF_EXPORT_FLOOR)
# The energy meter option for each side of the billing meter.
ENERGY_OPTIONS: tuple[tuple[Side, str], ...] = (
    ("import", CONF_IMPORT_ENERGY_ENTITY),
    ("export", CONF_EXPORT_ENERGY_ENTITY),
)


def signal_updated(entry_id: str) -> str:
    """Return the dispatcher signal sent after each recompute."""
    return f"{DOMAIN}_{entry_id}_updated"


def build_retailer_versions(
    raw_versions: list[Mapping[str, Any]],
) -> tuple[RetailerVersion, ...]:
    """Convert stored retailer versions (options) to dataclasses."""
    versions = [
        RetailerVersion(
            effective_from=date.fromisoformat(raw[CONF_EFFECTIVE_FROM]),
            **{name: raw[name] for name in RETAILER_FIELDS if name in raw},
        )
        for raw in raw_versions
    ]
    return tuple(sorted(versions, key=lambda v: v.effective_from))


def build_tariff(
    network: Network, data: Mapping[str, Any], options: Mapping[str, Any]
) -> Tariff:
    """Combine bundled network data, user overrides and the retailer plan."""
    overrides = list(options.get(CONF_NETWORK_OVERRIDES, []))
    return Tariff(
        network=apply_network_overrides(
            network.categories[data[CONF_PRICE_CATEGORY]], overrides
        ),
        loss=apply_loss_overrides(
            network.loss_categories[data[CONF_LOSS_CATEGORY]], overrides
        ),
        retailer=build_retailer_versions(options.get(CONF_RETAILER_VERSIONS, [])),
        gst=GST_RATE if data.get(CONF_GST_INCLUSIVE, True) else 1.0,
    )


@dataclass(frozen=True)
class PricePair:
    """Import and export price for one trading period."""

    period_start: datetime
    trading_period: int | None
    import_price: PriceBreakdown
    export_price: PriceBreakdown


@dataclass(frozen=True)
class ForecastRow:
    """One forecast period, priced for import and export."""

    period_start: datetime
    trading_period: int | None
    import_price: float
    export_price: float
    tou_band: str


def _spot(state: State | None) -> float | None:
    """Return the state's price in NZD/kWh, or None if it isn't usable."""
    if state is None:
        return None
    try:
        return to_nzd_per_kwh(
            float(state.state), state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        )
    except ValueError:
        return None


def parse_time(value: Any) -> datetime | None:
    """Parse a source timestamp; one without an offset is in HA's time zone."""
    if isinstance(value, str):
        value = dt_util.parse_datetime(value)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return value


def _trading_period(value: Any) -> int | None:
    try:
        number = int(value)
    except TypeError, ValueError:
        return None
    return number if 1 <= number <= PERIODS_PER_DAY_MAX else None


def _source_period(state: State, periods_back: int) -> datetime:
    """
    Return the start of the period a settled or live price is for.

    ``trading_period`` is authoritative; ``timestamp`` only says which day
    it is, so a timestamp that is the period end or publish time still
    prices correctly. Without a trading period the timestamp is taken as the
    period start (ha-electricityinfo-nz reports it that way). With neither,
    the source is taken to be ``periods_back`` periods behind when it last
    reported (settled holds N-1 during period N).

    The result depends only on the state, so repricing at a boundary never
    moves an unchanged value into the new period, while a source that
    re-reports the same value in a new period (``last_reported`` moves on)
    is priced for that period.
    """
    reported = state.last_reported
    timestamp = parse_time(state.attributes.get("timestamp"))
    trading_period = _trading_period(state.attributes.get("trading_period"))
    if trading_period is not None:
        start = trading_period_start(trading_period, timestamp or reported)
        if start is not None:
            return start
    if timestamp is not None:
        return period_start(timestamp)
    LOGGER.debug(
        "%s has no timestamp or trading period; assuming %s period(s) back",
        state.entity_id,
        periods_back,
    )
    return add_periods(period_start(reported), -periods_back)


def _row_period(
    stamp: datetime, trading_period: int | None
) -> tuple[datetime, int | None]:
    """
    Return the start of the period a forecast row is for, and its number.

    The stamp is the period start (ha-electricityinfo-nz stamps starts). The
    only correction is when ``trading_period`` is the period just before the
    stamp's: then the stamp is that period's end. A trading period that
    disagrees otherwise is dropped, so a misnumbered source can't move rows.
    """
    stamped = period_start(stamp)
    if trading_period is not None:
        start = trading_period_start(trading_period, stamp)
        if start is not None and start.astimezone(UTC) in (
            stamped.astimezone(UTC),
            add_periods(stamped, -1).astimezone(UTC),
        ):
            return start, trading_period
    return stamped, None


def _rows(value: Any) -> Sequence[Mapping[str, Any]]:
    """Return the dict rows of a forecast or history attribute."""
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _resolve_optional(hass: HomeAssistant, stored: str | None) -> str | None:
    return resolve_source(hass, stored) if stored else None


def _has_value(state: State) -> bool:
    """
    Return True once a source holds a real value.

    At startup HA writes an unavailable placeholder for entities that haven't
    loaded yet; that mustn't replace a restored price.
    """
    return state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


class BillingEngine:
    """Recompute landed prices on source changes and period boundaries."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tariff: Tariff,
        meter: BillingMeter | None = None,
        save: Callable[[], None] | None = None,
    ) -> None:
        """
        Initialise the engine for one config entry.

        ``meter`` is given when billing is set up; ``save`` then stores it.
        """
        self.hass = hass
        self.entry = entry
        self.tariff = tariff
        # Current entity ids; the entry stores registry ids, which follow renames.
        self.settled_entity = resolve_source(hass, entry.data[CONF_SETTLED_ENTITY])
        self.live_entity = _resolve_optional(hass, entry.data.get(CONF_LIVE_ENTITY))
        self.forecast_entity = _resolve_optional(
            hass, entry.data.get(CONF_FORECAST_ENTITY)
        )
        self.meter = meter
        self._save = save
        # Energy meters by side, by current entity id.
        self.energy_entities: dict[Side, str] = {}
        if meter is not None:
            for side, key in ENERGY_OPTIONS:
                if entity_id := _resolve_optional(hass, entry.options.get(key)):
                    self.energy_entities[side] = entity_id
        self.bill: Bill | None = None
        self.previous_bill: Bill | None = None
        self.settled: PricePair | None = None
        self.live: PricePair | None = None
        self.forecast: list[ForecastRow] = []
        self.forecast_current: ForecastRow | None = None
        self.forecast_available = False
        self.rates: Rates | None = None
        self.tou_band: str | None = None
        self.is_peak = False
        self.next_band_change: datetime | None = None
        # NZ date the next band change was last searched for; None = never.
        self._band_change_checked: date | None = None
        self.daily_charge: float | None = None
        # Price sensors keep their restored state until a source has reported.
        self.source_reported: set[str] = set()
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._unsub_timer: CALLBACK_TYPE | None = None

    @property
    def sources(self) -> list[str]:
        """Return the configured source entity ids."""
        return [
            e
            for e in (self.settled_entity, self.live_entity, self.forecast_entity)
            if e
        ]

    @callback
    def async_start(self) -> None:
        """Subscribe to sources, compute once and schedule the next boundary."""
        self._unsubscribers.append(
            async_track_state_change_event(self.hass, self.sources, self._handle_source)
        )
        # A source re-reporting an unchanged value is a new period's value.
        self._unsubscribers.append(
            async_track_state_report_event(
                self.hass,
                [e for e in (self.settled_entity, self.live_entity) if e],
                self._handle_source,
            )
        )
        self._unsubscribers.append(
            async_track_entity_registry_updated_event(
                self.hass,
                [*self.sources, *self.energy_entities.values()],
                self._handle_registry_update,
            )
        )
        if self.meter is not None:
            meters = list(self.energy_entities.values())
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, meters, self._handle_energy)
            )
            # An unchanged reading moves the meter's baseline on, so the next
            # increase isn't spread back over time when nothing was used.
            self._unsubscribers.append(
                async_track_state_report_event(self.hass, meters, self._handle_energy)
            )
            for side, entity_id in self.energy_entities.items():
                self._read_energy(side, self.hass.states.get(entity_id))
        if self.hass.state is CoreState.running:
            # A reload, not a restart: there's no startup to wait for.
            self.source_reported.update(self.sources)
        else:
            self._unsubscribers.append(
                async_at_started(self.hass, self._handle_started)
            )
        self._recompute()
        self._schedule_boundary()

    @callback
    def async_stop(self) -> None:
        """Remove listeners and the boundary timer."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _handle_started(self, _hass: HomeAssistant) -> None:
        """Stop holding restored prices for sources that never loaded."""
        self.source_reported.update(self.sources)
        self._recompute()

    @callback
    def _handle_registry_update(
        self, event: Event[EventEntityRegistryUpdatedData]
    ) -> None:
        """Reload to follow a renamed source to its new entity id."""
        if event.data["action"] == "update" and "entity_id" in event.data["changes"]:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    @callback
    def _handle_source(
        self, event: Event[EventStateChangedData] | Event[EventStateReportedData]
    ) -> None:
        """Reprice only what the changed or re-reported source feeds."""
        entity_id = event.data["entity_id"]
        current_start = period_start(dt_util.now())
        if entity_id == self.settled_entity:
            self._update_settled()
        if entity_id == self.live_entity:
            self._update_live()
        if entity_id == self.forecast_entity:
            self._update_forecast(current_start)
        if entity_id in (self.settled_entity, self.live_entity):
            self._update_bill()
        async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))

    @callback
    def _handle_energy(
        self, event: Event[EventStateChangedData] | Event[EventStateReportedData]
    ) -> None:
        """Count a new or re-reported energy meter reading."""
        side = next(
            s for s, e in self.energy_entities.items() if e == event.data["entity_id"]
        )
        if self._read_energy(side, event.data["new_state"]):
            self._update_bill()
            async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))
        elif self._save is not None:
            # The reading may still have moved the baseline on; keep that
            # over a restart. Pending saves are coalesced.
            self._save()

    def _read_energy(self, side: Side, state: State | None) -> bool:
        """Pass a usable reading to the meter; return True if the bill changed."""
        if self.meter is None or state is None or not _has_value(state):
            return False
        try:
            kwh = to_kwh(
                float(state.state), state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            )
        except ValueError:
            return False
        return self.meter.add_reading(side, kwh, state.last_reported)

    def _update_bill(self) -> None:
        """Recompute the cycle estimates and schedule a save."""
        if self.meter is None:
            return
        now = dt_util.now()
        # Start a new cycle when it's due, and make overdue periods final.
        self.meter.tick(now)
        self.bill = self.meter.bill(now)
        self.previous_bill = self.meter.bill(now, previous=True)
        if self._save is not None:
            self._save()

    @callback
    def _handle_boundary(self, _now: datetime) -> None:
        self._unsub_timer = None
        self._recompute()
        self._schedule_boundary()

    @callback
    def _schedule_boundary(self) -> None:
        """Wake at the next half hour: bands and versions only change there."""
        next_period = add_periods(period_start(dt_util.now()), 1)
        self._unsub_timer = async_track_point_in_time(
            self.hass, self._handle_boundary, next_period
        )

    def price_pair(
        self, spot: float, start: datetime, trading_period: int | None = None
    ) -> PricePair:
        """Price one period for import and export; raises NoRatesError."""
        import_breakdown, export_breakdown = prices(self.tariff, spot, start)
        return PricePair(
            period_start=start,
            trading_period=trading_period,
            import_price=import_breakdown,
            export_price=export_breakdown,
        )

    def _price_or_none(
        self, spot: float, start: datetime, trading_period: int | None
    ) -> PricePair | None:
        """Price a period, or None when rate data doesn't cover it."""
        try:
            return self.price_pair(spot, start, trading_period)
        except NoRatesError:
            # No price beats a too-low one.
            return None

    @callback
    def _recompute(self) -> None:
        """Reprice everything; bands and versions only change on a boundary."""
        now = dt_util.now()
        current_start = period_start(now)
        self._update_settled()
        self._update_live()
        self._update_forecast(current_start)

        # One rule for every tariff sensor (band, peak, next band change,
        # daily charge, like the prices): without every rate in force now,
        # they're unavailable rather than half-known. Entities check
        # self.rates for it.
        self.rates = rates = rates_at(self.tariff, now)
        if rates is not None:
            self.tou_band = rates.band
            self.is_peak = rates.band in rates.network.bands.peak
            self.daily_charge = rates.daily_charge(self.tariff.gst)
        else:
            LOGGER.debug("Rate data doesn't cover %s", now.isoformat())
            self.tou_band = None
            self.is_peak = False
            self.daily_charge = None
        # The scan can cover months, so only redo it once the change has
        # passed, or daily while none was found within the search horizon.
        today = now.astimezone(NZ_TZ).date()
        if (
            self._band_change_checked is None
            # next_band_change is in UTC, so this compares absolute times.
            or (self.next_band_change is not None and now >= self.next_band_change)
            or (self.next_band_change is None and today != self._band_change_checked)
        ):
            self.next_band_change = next_tariff_band_change(self.tariff.network, now)
            self._band_change_checked = today
        self._update_bill()
        async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))

    def _source_state(self, entity_id: str) -> State | None:
        state = self.hass.states.get(entity_id)
        if state is not None and _has_value(state):
            # Once the source has a value, its state wins over a restored one.
            self.source_reported.add(entity_id)
        return state

    def _price_source(self, entity_id: str, periods_back: int) -> PricePair | None:
        """Price a settled or live source for the period its value is for."""
        state = self._source_state(entity_id)
        spot = _spot(state)
        if state is None or spot is None:
            return None
        start = _source_period(state, periods_back)
        pair = self._price_or_none(
            spot, start, _trading_period(state.attributes.get("trading_period"))
        )
        if pair is None:
            LOGGER.debug("No rate data covers %s for %s", start.isoformat(), entity_id)
        return pair

    def _update_settled(self) -> None:
        self.settled = self._price_source(self.settled_entity, 1)
        self._settle_billing()

    def _update_live(self) -> None:
        if self.live_entity:
            self.live = self._price_source(self.live_entity, 0)
            if self.meter is not None and self.live is not None:
                self.meter.set_live_spot(
                    self.live.period_start, self.live.import_price.spot
                )

    def _settle_billing(self) -> None:
        """
        Price metered periods the settled source has a price for.

        The source's history covers about a day, so periods missed while HA
        was down still settle. Settling an already priced period does nothing.
        """
        if self.meter is None:
            return
        state = self.hass.states.get(self.settled_entity)
        if state is None or not _has_value(state):
            return
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        for raw in _rows(state.attributes.get("history")):
            stamp = parse_time(raw.get("period_start"))
            if stamp is None:
                continue
            start, _ = _row_period(stamp, _trading_period(raw.get("trading_period")))
            try:
                spot = to_nzd_per_kwh(float(raw["price"]), unit)
            except KeyError, TypeError, ValueError:
                continue
            self.meter.settle(start, spot)
        if (settled := self.settled) is not None:
            spot = settled.import_price.spot
            self.meter.settle(settled.period_start, spot)
            if not self.live_entity:
                # Without a live source, the latest settled spot is the best
                # guess for periods that haven't settled.
                self.meter.latest_spot = spot

    def _update_forecast(self, current_start: datetime) -> None:
        if not self.forecast_entity:
            return
        state = self._source_state(self.forecast_entity)
        if state is None or state.state == STATE_UNAVAILABLE:
            self.forecast = []
            self.forecast_current = None
            self.forecast_available = False
            return
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        # The source lists the current period as the last history row and
        # only later periods under "forecast".
        raw_rows = [
            *_rows(state.attributes.get("history"))[-1:],
            *_rows(state.attributes.get("forecast")),
        ]
        # Keyed by period start in UTC: a stamp off the half hour still lands
        # on its period, and equality across zones fails in the repeated DST
        # hour.
        rows: dict[datetime, ForecastRow] = {}
        listed: set[datetime] = set()
        unpriced = 0
        now_key = current_start.astimezone(UTC)
        for raw in raw_rows:
            stamp = parse_time(raw.get("period_start"))
            if stamp is None:
                continue
            start, trading_period = _row_period(
                stamp, _trading_period(raw.get("trading_period"))
            )
            key = start.astimezone(UTC)
            # In UTC: within one zone Python ignores the DST fold, so a
            # first-pass repeated-hour period wouldn't count as past.
            if key < now_key:
                continue
            # Listed even without a usable price, so the next period can't
            # stand in for it.
            listed.add(key)
            price = raw.get("price")
            if price is None:
                continue
            try:
                spot = to_nzd_per_kwh(float(price), unit)
            except TypeError, ValueError:
                continue
            pair = self._price_or_none(spot, start, trading_period)
            if pair is None:
                unpriced += 1
                continue
            rows[key] = ForecastRow(
                period_start=start,
                trading_period=pair.trading_period,
                import_price=pair.import_price.price,
                export_price=pair.export_price.price,
                tou_band=pair.import_price.tou_band,
            )
        if unpriced:
            LOGGER.debug("No rate data covers %s forecast period(s)", unpriced)
        self.forecast = [rows[k] for k in sorted(rows)]
        # The state is the current period, or the next one only when the
        # source doesn't list the current one; never a later row standing in
        # for a period that was listed but couldn't be priced.
        self.forecast_current = rows.get(now_key)
        if self.forecast_current is None and now_key not in listed:
            self.forecast_current = rows.get(
                add_periods(current_start, 1).astimezone(UTC)
            )
        # Available while any period is priced, so the list stays readable;
        # the state is unknown when the current period has no price.
        self.forecast_available = bool(self.forecast)
