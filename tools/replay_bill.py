"""
Reconstruct a billing cycle's bill from recorded grid power and prices.

Replays a signed grid power series (W, positive import, negative export)
through the integration's BillingMeter, prices each trading period at its
settled spot with the entry's tariff, and prints the bill's lines. Use it to
check the integration against a past invoice.

The data directory holds InfluxDB 1.x query responses (``epoch=s``), as the
Home Assistant InfluxDB integration stores states, plus the config entry:

- ``entry.json``: ``{"data": …, "options": …}`` of the electricity_billing_nz
  entry, from ``.storage/core.config_entries``.
- ``power.json``: ``SELECT value FROM "W" WHERE entity_id = '<grid power>'``
  over the cycle, starting a little before it so the first value is known.
- ``settled.json``: ``SELECT timestamp_str, trading_period, value FROM
  "c/kWh" WHERE entity_id = '<node>_c_kwh_settled_price'`` over the cycle.
- ``history.json`` (optional): ``SELECT history_str FROM …`` for the same
  sensor; fills periods the settled series missed.

Power holds from one stored value to the next, as the integration integrates
it. The InfluxDB integration stores unavailable states outside the ``W``
series, so an outage (Home Assistant or the inverter's data down) reads as
the last value held. Mark it with ``--gap SINCE,UNTIL``: readings inside it
are ignored, nothing is counted until the first reading at or after UNTIL
(set UNTIL to that reading), and energy known from elsewhere (e.g. the
inverter's daily totals) can be spread evenly over it.

    uv run --group dev python -m tools.replay_bill --month 2026-10 --data DIR
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import State

from custom_components.electricity_billing_nz.billing import (
    BillingMeter,
    next_cycle_start,
)
from custom_components.electricity_billing_nz.const import (
    CONF_BILLING_CYCLE_DAY,
    DEFAULT_BILLING_CYCLE_DAY,
)
from custom_components.electricity_billing_nz.engine import (
    _row_period,
    _source_period,
    _trading_period,
    build_tariff,
)
from custom_components.electricity_billing_nz.networks import (
    load_networks,
    network_for_entry,
)
from custom_components.electricity_billing_nz.tariff import (
    NZ_TZ,
    nz_midnight,
    to_nzd_per_kwh,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from custom_components.electricity_billing_nz.billing import Bill, Side
    from custom_components.electricity_billing_nz.tariff import Tariff

PERIOD = timedelta(minutes=30)


@dataclass(frozen=True)
class Gap:
    """A recording gap: unavailable from ``since``, with energy known to fill it."""

    since: datetime
    until: datetime
    import_kwh: float = 0.0
    export_kwh: float = 0.0


@dataclass(frozen=True)
class Replay:
    """A replayed cycle's bill, and the periods with energy but no settled spot."""

    bill: Bill
    unsettled: list[datetime]


def query_rows(path: Path) -> list[list[Any]]:
    """Return every value row of an InfluxDB 1.x query response."""
    response = json.loads(path.read_text())
    rows: list[list[Any]] = []
    for result in response.get("results", []):
        if "error" in result:
            msg = f"{path.name}: InfluxDB error: {result['error']}"
            raise ValueError(msg)
        for series in result.get("series", []):
            rows.extend(series["values"])
    if "error" in response:
        msg = f"{path.name}: InfluxDB error: {response['error']}"
        raise ValueError(msg)
    return rows


def _nz_time(value: Any) -> datetime | None:
    """Parse an ISO time; one without an offset is NZ time."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=NZ_TZ)


def _settled_period(stamp: Any, trading_period: Any) -> datetime | None:
    """Resolve a stored settled state's period as the engine resolves it."""
    if (timestamp := _nz_time(stamp)) is None:
        return None
    state = State(
        "sensor.settled",
        "0",
        {"timestamp": timestamp.isoformat(), "trading_period": trading_period},
        last_reported=timestamp,
    )
    return _source_period(state, 1).astimezone(UTC)


def _history_period(stamp: Any, trading_period: Any) -> datetime | None:
    """Resolve a history row's period as the engine resolves it."""
    if (timestamp := _nz_time(stamp)) is None:
        return None
    start, _ = _row_period(timestamp, _trading_period(trading_period))
    return start.astimezone(UTC)


def settled_prices(
    rows: Iterable[Sequence[Any]],
    history: Iterable[Sequence[Any]] = (),
    unit: str = "c/kWh",
) -> dict[datetime, float]:
    """
    Return the settled spot by period start (UTC), in NZD/kWh.

    ``rows`` are (time, timestamp_str, trading_period, value); ``history``
    rows are (time, history_str), whose entries only fill periods ``rows``
    lack. Periods resolve as the integration resolves them: for the settled
    state, its trading period wins and the timestamp gives the day.
    """
    prices: dict[datetime, float] = {}
    for _time, stamp, trading_period, value in rows:
        start = _settled_period(stamp, trading_period)
        if start is not None and value is not None:
            prices[start] = to_nzd_per_kwh(float(value), unit)
    for _time, text in history:
        try:
            entries = ast.literal_eval(text or "[]")
        except ValueError, SyntaxError:
            continue
        for entry in entries:
            try:
                start = _history_period(
                    entry["period_start"], entry.get("trading_period")
                )
                price = to_nzd_per_kwh(float(entry["price"]), unit)
            except KeyError, TypeError, ValueError:
                continue
            if start is not None:
                prices.setdefault(start, price)
    return prices


def cycle_bounds(month: str, cycle_day: int) -> tuple[datetime, datetime]:
    """Return the NZ start of the cycle starting in a YYYY-MM month, and its end."""
    start = date.fromisoformat(f"{month}-01").replace(day=cycle_day)
    return nz_midnight(start), nz_midnight(next_cycle_start(start))


def replay(  # noqa: PLR0913
    tariff: Tariff,
    power: Iterable[Sequence[Any]],
    prices: Mapping[datetime, float],
    month: str,
    *,
    cycle_day: int = DEFAULT_BILLING_CYCLE_DAY,
    gaps: Iterable[Gap] = (),
) -> Replay:
    """
    Return the cycle's bill, as the integration would have metered it.

    ``power`` rows are (epoch seconds, signed W, or None while unavailable).
    """
    start, end = cycle_bounds(month, cycle_day)
    meter = BillingMeter(
        tariff, cycle_day, sources={"import": "grid", "export": "grid"}
    )
    gaps = sorted(gaps, key=lambda g: g.since)
    started = list(gaps)  # gaps whose start hasn't been reached
    for epoch, watts in sorted(power, key=lambda row: row[0]):
        at = datetime.fromtimestamp(epoch, UTC)
        while started and started[0].since <= at:
            _stop(meter, started.pop(0).since)
        if any(gap.since <= at < gap.until for gap in gaps):
            continue  # inside a gap: not counted
        if watts is None:
            _stop(meter, at)
            continue
        meter.add_power("import", watts / 1000, at)
        meter.add_power("export", -watts / 1000, at)
    for gap in started:  # after the last reading
        _stop(meter, gap.since)
    meter.advance(end)
    for gap in gaps:
        known: tuple[tuple[Side, float], ...] = (
            ("import", gap.import_kwh),
            ("export", gap.export_kwh),
        )
        for side, kwh in known:
            if kwh > 0:
                meter.add_energy(side, gap.since, gap.until, kwh)

    period = start.astimezone(UTC) - PERIOD
    while period < end:
        if period in prices:
            meter.settle(period, prices[period])
            meter.latest_spot = prices[period]
        elif (usage := meter.pending.get(period)) is not None:
            # Without a live source, the integration prices a period that
            # hasn't settled at the latest settled spot at the time.
            usage.live_spot = meter.latest_spot
        period += PERIOD
    unsettled = sorted(
        key
        for key, usage in meter.pending.items()
        if start <= key < end and (usage.import_kwh > 0 or usage.export_kwh > 0)
    )
    bill = meter.bill(end - timedelta(seconds=1))
    if bill is None:
        msg = f"No power recorded for the cycle starting in {month}"
        raise ValueError(msg)
    return Replay(bill, unsettled)


def _stop(meter: BillingMeter, at: datetime) -> None:
    """Stop counting held power at ``at``, as when the sensor is unavailable."""
    meter.mark_unavailable("import", at)
    meter.mark_unavailable("export", at)


def load_tariff(entry: Mapping[str, Any], import_margin: float | None) -> Tariff:
    """Return the entry's tariff, optionally with every import margin replaced."""
    data, options = entry["data"], dict(entry["options"])
    if import_margin is not None:
        if not options.get("retailer_versions"):
            msg = "--import-margin needs the entry's retailer versions"
            raise ValueError(msg)
        options["retailer_versions"] = [
            {**version, "import_margin": import_margin}
            for version in options.get("retailer_versions", [])
        ]
    found = network_for_entry(load_networks(), data)
    if found is None:
        msg = "The entry's network or categories aren't in the bundled data"
        raise ValueError(msg)
    return build_tariff(found.network, data, options)


def _gap(value: str) -> Gap:
    parts = value.split(",")
    if len(parts) not in (2, 4):
        msg = "expected SINCE,UNTIL or SINCE,UNTIL,IMPORT_KWH,EXPORT_KWH"
        raise argparse.ArgumentTypeError(msg)
    since, until = _nz_time(parts[0]), _nz_time(parts[1])
    if since is None or until is None:
        msg = "SINCE and UNTIL must be ISO times"
        raise argparse.ArgumentTypeError(msg)
    if until <= since:
        msg = "UNTIL must be after SINCE"
        raise argparse.ArgumentTypeError(msg)
    kwh = [float(p) for p in parts[2:]] or [0.0, 0.0]
    return Gap(since, until, *kwh)


def main(argv: list[str] | None = None) -> int:
    """Print the reconstructed bill for a billing cycle."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--month", required=True, help="YYYY-MM: the cycle starting in it"
    )
    parser.add_argument("--data", required=True, type=Path, help="data directory")
    parser.add_argument(
        "--import-margin", type=float, help="NZD/kWh, replacing the entry's"
    )
    parser.add_argument(
        "--price-unit", default="c/kWh", help="unit of the settled series"
    )
    parser.add_argument(
        "--gap",
        action="append",
        default=[],
        type=_gap,
        metavar="SINCE,UNTIL[,IMPORT_KWH,EXPORT_KWH]",
        help="a recording gap (ISO times, NZ if no offset), with energy to "
        "spread evenly over it",
    )
    args = parser.parse_args(argv)

    entry = json.loads((args.data / "entry.json").read_text())
    tariff = load_tariff(entry, args.import_margin)
    history = args.data / "history.json"
    prices = settled_prices(
        query_rows(args.data / "settled.json"),
        query_rows(history) if history.exists() else (),
        args.price_unit,
    )
    result = replay(
        tariff,
        query_rows(args.data / "power.json"),
        prices,
        args.month,
        cycle_day=int(
            entry["options"].get(CONF_BILLING_CYCLE_DAY, DEFAULT_BILLING_CYCLE_DAY)
        ),
        gaps=args.gap,
    )
    bill = result.bill
    print(json.dumps(bill.lines(), indent=2))  # noqa: T201
    print(  # noqa: T201
        f"{bill.start} to {bill.end}: import {bill.import_kwh:.2f} kWh, "
        f"export {bill.export_kwh:.2f} kWh, subtotal {bill.subtotal:.2f} ex GST, "
        f"total {bill.total:.2f}"
    )
    if result.unsettled:
        print(  # noqa: T201
            f"{len(result.unsettled)} periods with energy had no settled price; "
            "they're priced at the latest settled spot",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
