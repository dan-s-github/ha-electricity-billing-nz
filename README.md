# Electricity Billing NZ

[![Marketplace][marketplacebadge]][marketplace]
[![Home Assistant][ha-shield]][ha]
[![Python Version][python-shield]][python]
[![License][license-shield]](LICENSE)
[![Tests][tests-shield]][tests]
[![Code Style: Ruff][ruff-shield]][ruff]

A Home Assistant custom integration that works out what you actually pay per
kWh on a New Zealand wholesale (spot) pass-through electricity plan. It is a
companion to [ha-electricityinfo-nz](https://github.com/dan-s-github/ha-electricityinfo-nz),
which provides the wholesale node price. This integration adds your lines
company's network charges and your retailer's charges to that price.

- Domain: `electricity_billing_nz`
- IoT class: `calculated` (reads other entities and bundled rate data; the
  only network call is the optional ICP lookup during setup)

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
| Transmission (per kWh) | Retailer (recovers the lines company's monthly grid charge) | import |
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
export(t) = max(spot(t) − export_margin + export_credit(t), export_floor) × GST
```

`export_floor` is the optional minimum export price; without one, export can
go negative with spot.

`network(t)` and `export_credit(t)` depend on the time-of-use period (peak or
off-peak) that period *t* falls in, using the lines company's peak windows.

## Features

- **Config flow**: choose the ha-electricityinfo-nz price sensors (settled,
  and optionally live and day-ahead forecast), a lines company with its price
  category and loss category, and your retailer plan (margins, transmission,
  levy, daily charges).
- **Bundled network rates**: one YAML file per lines company, versioned by
  effective date. Vector is included from 1 April 2025 (price categories
  `ARHLC` and `ARHLU`, loss category `VECA1`). More networks can be added by PR.
- **Rate versions**: add a retailer rate change before it takes effect, or
  override bundled network values from a date. Past periods keep the rates
  that applied at the time.
- **Sensors** (prices in NZD/kWh, including GST unless you turn it off):
  - import and export price for the settled period, to match the bill; usable
    as the Energy dashboard price entities. The ex-GST components are
    attributes.
  - live import and export price, for decisions such as battery charging
    (needs the live sensor), with the same attributes
  - import and export price forecast, with a per-period `forecast` attribute
    for EMHASS and similar optimisers (needs the forecast sensor)
  - daily charge (NZD/day)
  - current time-of-use band and when it next changes
  - diagnostics: each rate in force now, ex GST, to check against your bill
    or plan: import and export margins, transmission, levy, metering and
    retailer daily charges, the network rate and export credit for the
    current band (every band's as attributes), the network daily charge and
    the loss factor. Each shows the date its rate version took effect.
- **Billing cycle** (optional, needs grid import and export power or energy
  sensors):
  - import and export kWh this cycle, per time-of-use band
  - average import and export energy rates (ex GST), to set against the
    bill's energy lines
  - an estimated bill total, with the invoice lines as attributes: daily
    charges, network per band, energy, transmission, levy, export and GST,
    and the previous cycle's
  - import cost and export earnings totals since billing was set up
  - diagnostic: when metering of the current cycle began, whether the cycle
    is complete, and the meter entities configured
- **Binary sensor**: on during peak.
- **Services**: `price_at` (the price for a given time and spot price, for
  backtests) and `export_versions` (the rate versions in force, as YAML).

Planned: more lines companies.

## Installation

Once there is a first release:

### Marketplace (recommended)

Home Assistant 2026.11 or newer:

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dan-s-github&repository=ha-electricity-billing-nz&category=Integration)

Or by hand:

1. Go to Settings -> Marketplace and select **Add from link**.
2. Enter `dan-s-github/ha-electricity-billing-nz` and select **Add**.
3. Open the repository's page, select **Install**, then restart Home Assistant.
4. Add the integration from Settings -> Devices & Services.

On Home Assistant 2026.10 or older, add it as a custom repository in HACS
instead.

### Manual

1. Copy `custom_components/electricity_billing_nz` into your Home Assistant
   `custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from Settings -> Devices & Services.

Requires Home Assistant 2026.9.0 or newer and
[ha-electricityinfo-nz](https://github.com/dan-s-github/ha-electricityinfo-nz)
for the wholesale price.

## Configuration

1. Set up [ha-electricityinfo-nz](https://github.com/dan-s-github/ha-electricityinfo-nz)
   for your ICP's node first.
2. Go to Settings -> Devices & Services, select **Add integration** and pick
   **Electricity Billing NZ**.
3. **Spot price source**: name the entry and pick the settled price sensor,
   plus the live and day-ahead forecast sensors if you want those prices. The
   ICP number from your bill is optional; with it, the same connection can't
   be added twice. Leave it empty to add a second entry for the same node, for
   example to compare plans.
4. **Lines company**, then **Categories**: pick the price category and loss
   category on your ICP's Electricity Authority registry record (or the ones
   your bill charges for). If you entered an ICP number, setup looks up the
   record on the Authority's public
   [Your meter](https://www.ea.govt.nz/your-power/your-meter/) page and
   pre-selects the lines company and both categories. It also warns if the
   record's point of connection isn't the settled sensor's node. If the page
   can't be read, pick them yourself.
5. **Network rates**: the lines company's daily charge, distribution rates,
   export credits and loss factor for those categories, from the bundled
   schedule in force today, ex GST. Check them against the lines charges on
   your bill. A value you change is saved as a network override from that
   schedule's start until the next bundled schedule.
6. **Retailer plan**: enter the per-kWh values in c/kWh and the daily charges
   in NZD/day, all excluding GST. **Prices include GST** (on by default) adds
   GST to the prices and daily charge the integration reports; turn it off
   for ex-GST prices. You can change it later under **GST** in the options.

When a rate changes later, open the entry's **Configure** menu:

- **Add retailer rate version**: new retailer values from a date.
- **Override network values**: replace bundled network values from a date
  until the next bundled schedule, e.g. when your bill differs. The date
  can't be before the bundled data starts.
- **Change source entities**: the live and forecast sensors. The settled
  sensor is fixed; to change it, re-add the entry.
- **Billing cycle**: the grid import and export meters and the day of the
  month your bill starts on (1 to 28). Each meter is a power sensor (W, kW or
  MW) or an energy meter (Wh, kWh or MWh, a running total with state class
  `total` or `total_increasing`). Instead of the two, you can choose one
  signed grid power sensor: positive when importing, negative when exporting.
  See [Choosing the meters](#choosing-the-meters).
- **ICP number**.
- **GST**: whether the reported prices include GST. Changing it moves the
  price history by 15% from then on, and `price_at` uses the current setting
  for every period, past ones included.

To check a price, call `electricity_billing_nz.price_at` from Developer Tools ->
Actions:

```yaml
action: electricity_billing_nz.price_at
data:
  config_entry_id: <your entry>
  timestamp: "2026-07-15T17:00:00+12:00"
  spot: 0.12
  spot_unit: NZD/kWh
```

The response has `period_start` (the start of the trading period priced),
`import_price` and `export_price` in NZD/kWh, and the ex-GST components under
`import` and `export`, the same as the price sensors' attributes. Periods
before the bundled network data starts can't be priced.

### Billing cycle

With the meters set, each trading period's energy is priced at that
period's settled spot, with the rates in force then, so the estimate follows
the bill's half-hourly pricing.

- Energy counts as soon as the meter reports it. Until its period settles, it
  is priced at the live spot and counted as provisional
  (`provisional_periods`).
- A period with no settled price after 48 hours keeps the live price and is
  counted in `unsettled_periods`.
- The cycle that was running when you set billing up is partial: the total's
  `complete` attribute is false until the first full cycle. The same goes
  for a meter added during a cycle (an export meter where there was none).
- Bills use the final wholesale prices, published days later; the settled
  prices are interim, so expect small differences.
- The estimate includes GST unless the entry's GST option is off.
- The meter is stored with the entry and survives restarts. With an energy
  meter, a gap while Home Assistant was down is shared evenly over the
  periods it covers; with a power sensor, it isn't counted.
- An energy meter's increase between two readings is shared evenly over the
  time between them. A meter that reports its unchanged value marks time
  when nothing was used; one that only reports changes has its energy
  spread back to the last change. A power sensor's value holds until the
  next, and is counted into each period as it ends.
- A rate version or network override dated into the current or previous
  cycle reprices it when saved. The lifetime cost and earnings totals keep
  what each period cost when it settled.
- A reading more than 10% below the last one is a meter reset (e.g. a daily
  total); a smaller drop is ignored.
- Replacing a meter (say, an energy meter with a power sensor) starts it
  from a new baseline, and the cycle stays as complete as it was: the energy
  and daily charges already counted are kept. Energy between the old
  meter's last reading and the new one's first isn't counted. Changing the cycle day
  moves the kept periods into the new day's cycles; the earliest is marked
  incomplete.

#### Choosing the meters

Each half-hour's energy is priced at that half-hour's spot, so billing needs
to know when the energy was used.

- **Power sensors (best):** billing integrates them itself, holding each
  value until the next. A sensor that only reports changes can sit at 0 W for
  hours (a battery covering the house overnight, say) and the energy still
  lands in exactly the right half-hours. Use the grid import and export power,
  or one signed grid power sensor. Energy isn't counted while Home Assistant
  is down or the sensor is unavailable, as the power then is unknown.
- **Energy meters:** a smart meter or CT clamp total that updates at least
  every half hour. Energy between two readings is shared evenly over the gap,
  so a meter that only reports changes, or in coarse steps, puts it in the
  wrong half-hours. Energy used while Home Assistant was down is caught up.
- **Not suitable:** daily or cloud statistics that change in coarse steps or
  only a few times a day. A step reported mid-morning is shared evenly back
  to the previous one, often overnight, and priced at the wrong spots.

As a check, if an energy meter goes more than an hour without a reading and
then reports at least 0.5 kWh, the entry logs a warning and raises a Repairs
issue naming the meter. It doesn't catch every meter that's too slow, so
check yours updates often enough. A gap while the meter was unavailable or
Home Assistant was down doesn't count. The issue is cleared when the entry
reloads (as choosing a different meter does) or Home Assistant restarts, and
comes back if the meter still reports too rarely.

## Not in scope

- Fetching wholesale prices (use ha-electricityinfo-nz).
- Logging in to retailer or lines-company accounts.
- Fixed-price retail plans (maybe later).

## Roadmap

1. Vector network, one retailer plan, price sensors and `price_at` (done).
2. Bill reconciliation sensors and billing-cycle options (done).
3. More lines companies and a marketplace release.

## Development

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.6 or newer.

```bash
./scripts/setup  # creates uv.lock; commit it, the GitHub workflows need it
./scripts/lint
uv run --group dev pytest
./scripts/develop  # runs Home Assistant with this integration
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the release process and CI.

[marketplace]: https://www.home-assistant.io/integrations/marketplace/
[marketplacebadge]: https://img.shields.io/badge/Marketplace-Custom-orange.svg?style=flat&logo=homeassistant&logoColor=white
[ha-shield]: https://img.shields.io/badge/Home%20Assistant-2026.9.0+-blue.svg?style=flat&logo=homeassistant&logoColor=white
[ha]: https://www.home-assistant.io/
[python-shield]: https://img.shields.io/badge/python-3.14+-blue.svg?style=flat&logo=python&logoColor=white
[python]: https://www.python.org/
[license-shield]: https://img.shields.io/github/license/dan-s-github/ha-electricity-billing-nz.svg?style=flat
[tests-shield]: https://img.shields.io/github/actions/workflow/status/dan-s-github/ha-electricity-billing-nz/ci.yml?branch=main&style=flat&logo=github
[tests]: https://github.com/dan-s-github/ha-electricity-billing-nz/actions/workflows/ci.yml
[ruff-shield]: https://img.shields.io/badge/code%20style-ruff-000000.svg?style=flat&logo=ruff&logoColor=white
[ruff]: https://github.com/astral-sh/ruff
