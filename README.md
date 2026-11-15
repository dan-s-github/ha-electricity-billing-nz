# Electricity Billing NZ

> **Status: planned.** Nothing is implemented yet. This README describes what
> the integration will do. The repo will be scaffolded from
> [ha-custom-integration-template](https://github.com/dan-s-github/ha-custom-integration-template).

A Home Assistant custom integration that works out what you actually pay per
kWh on a New Zealand wholesale (spot) pass-through electricity plan. It is a
companion to [ha-electricityinfo-nz](https://github.com/dan-s-github/ha-electricityinfo-nz),
which provides the wholesale node price. This integration adds your lines
company's network charges and your retailer's charges to that price.

- Domain: `electricity_billing_nz`
- IoT class: `calculated` (no network calls; reads other entities and bundled rate data)

## Why

A spot-plan bill is more than the wholesale price. Each half-hour's import
price is built from:

| Component | Set by | Applies to |
| --- | --- | --- |
| Wholesale node price | Electricity market | import and export |
| Loss factor | Lines company (loss category) | import energy only |
| Retailer margin (flat c/kWh) | Retailer | import (+) and export (−) |
| Distribution, time of use | Lines company (price category) | import |
| Export credit, time of use | Lines company (price category) | export |
| Transmission | Lines company | import |
| Electricity Authority levy | Retailer | import |
| Daily charges (network, metering, retailer) | Lines company / retailer | per day |
| GST | — | everything, applied last |

Doing this with template sensors means copying the same constants into many
templates and editing all of them every 1 April when network prices change.
This integration keeps the rates in one place, with effective dates.

## Price model

For each trading period *t*, in NZD/kWh:

```
import(t) = ((spot(t) + import_margin) × loss_factor + network(t) + transmission + levy) × GST
export(t) = (spot(t) − export_margin + export_credit(t)) × GST
```

`network(t)` and `export_credit(t)` depend on the time-of-use period (peak or
off-peak) that period *t* falls in, using the lines company's peak windows.

## Planned features

- **Config flow**: choose the ha-electricityinfo-nz price entities (settled,
  live, day-ahead forecast), a lines company with its price category and loss
  category (the codes on your ICP's Electricity Authority registry record), and
  enter your retailer plan (margins, daily charges, levy).
- **Bundled network rates**: one YAML file per lines company, versioned by
  effective date, starting with Vector. More networks can be added by PR.
- **Rate versions**: enter a new schedule before it takes effect; past periods
  keep the rates that applied at the time.
- **Sensors**:
  - landed import and export price (settled, to match the bill; usable as the
    Energy dashboard price entity)
  - live import and export price (for decisions such as battery charging)
  - import and export price forecast, with a per-period `forecast` attribute
    (for EMHASS and similar optimisers)
  - fixed daily charge
  - current time-of-use period and when it next changes
  - billing-cycle average energy rates and estimated bill total, to check
    against your bill
- **Binary sensor**: peak period on/off.
- **Services**: `price_at` (price for a given time and spot price, for
  backtests) and `export_versions` (dump the configured rate versions).

## Not in scope

- Fetching wholesale prices (use ha-electricityinfo-nz).
- Logging in to retailer or lines-company accounts.
- Fixed-price retail plans (maybe later).

## Roadmap

1. Vector network, one retailer plan, price sensors and `price_at`.
2. Bill reconciliation sensors and billing-cycle options.
3. More lines companies and a HACS release.
