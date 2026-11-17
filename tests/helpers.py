"""Shared helpers for tests."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.core import HomeAssistant

from custom_components.electricity_billing_nz.tariff import (
    NZ_TZ,
    add_periods,
    nz_midnight,
)

from .conftest import FORECAST, LIVE, SETTLED


def nz(text: str) -> datetime:
    """Return a Pacific/Auckland datetime from 'YYYY-MM-DD HH:MM'."""
    return datetime.fromisoformat(text).replace(tzinfo=NZ_TZ)


def trading_period(start: datetime) -> int:
    """Return the NZ trading period number (1 at midnight) for a period start."""
    midnight = nz_midnight(start.astimezone(NZ_TZ).date())
    # In UTC: within one zone, subtraction ignores a DST change in between.
    elapsed = start.astimezone(UTC) - midnight.astimezone(UTC)
    return int(elapsed.total_seconds() // 1800) + 1


def template_import(
    spot: float,
    *,
    peak: bool | None = None,
    network: float | None = None,
    loss: float | None = None,
) -> float:
    """
    Port of the margin-aware electricity_import_price template.

    Give peak for Vector 2026 ARHLC, or network and loss (not both) for
    another year.
    """
    if (peak is None) == (network is None):
        msg = "Give exactly one of peak or network"
        raise TypeError(msg)
    if (network is None) != (loss is None):
        msg = "Give loss with network only: it changes with the year too"
        raise TypeError(msg)
    if network is None or loss is None:
        network = 0.1504 if peak else 0.0457
        loss = 1.0476
    return ((spot + 0.0286) * loss + network + 0.0302 + 0.002) * 1.15


def template_export(spot: float, *, peak: bool) -> float:
    """Port of the margin-aware electricity_export_price template."""
    return (spot - 0.019 + (0.0524 if peak else 0)) * 1.15


def set_settled(hass: HomeAssistant, cents: float | str, period: datetime) -> None:
    """Set the settled source, as ha-electricityinfo-nz reports it."""
    hass.states.async_set(
        SETTLED,
        str(cents),
        {
            "unit_of_measurement": "c/kWh",
            "timestamp": period.isoformat(),
            "trading_period": trading_period(period),
            "node": "ROS0221",
        },
    )


def set_live(hass: HomeAssistant, cents: float | str, period: datetime) -> None:
    """Set the live source for the period it's for."""
    hass.states.async_set(
        LIVE,
        str(cents),
        {
            "unit_of_measurement": "c/kWh",
            "timestamp": period.isoformat(),
            "trading_period": trading_period(period),
        },
    )


def set_forecast(hass: HomeAssistant, start: datetime, cents: list[float]) -> None:
    """Set the forecast source: the first price is the current period."""
    starts = [add_periods(start, i) for i in range(len(cents))]
    rows = [
        {
            "period_start": period.isoformat(),
            "trading_period": trading_period(period),
            "price": price,
        }
        for period, price in zip(starts, cents, strict=True)
    ]
    hass.states.async_set(
        FORECAST,
        str(cents[0]),
        {"unit_of_measurement": "c/kWh", "history": rows[:1], "forecast": rows[1:]},
    )
