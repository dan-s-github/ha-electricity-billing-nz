# Implementation plan: Electricity Billing NZ (`electricity_billing_nz`)

## Context

The spec ([Spec: ha-electricity-billing-nz](https://claude.ai/artifact/VhGa3Ls1hWw9ftLyYJbYCk)) describes a calculated HA integration. It turns the ha-electricityinfo-nz node price into the landed import/export price on a spot pass-through plan. It replaces about 10 Jinja templates that each hardcode the Vector/Ecotricity constants: `packages/electricity_pricing.yaml`, `packages/emhass.yaml` and `tools/emhass-backtest/backtest.py` in the `electricity-pricing` repo.

Where the repo stands now:
- `main` has the copier scaffold (PR #1, from `ha-custom-integration-template` @7aed512). It includes `custom_components/electricity_billing_nz/` with a placeholder `Status` sensor, a name-only config flow, a `holidays` requirement, uv/pytest-homeassistant-custom-component 0.13.375 (HA 2026.10.4), ruff/mypy and CI.
- Margin-aware templates (+2.86 c import, −1.90 c export) exist only as uncommitted edits in the `electricity-pricing/ecotricity-margin` worktree. The `dan-dev` templates have no margin.

This plan covers **Phase 1 in detail**: personal use, replacing the templates. Phases 2 and 3 are sketched at the end.

## Facts from the upstream integration (`ha-electricityinfo-nz/main/.../sensor.py`)

- Price sensor unit is `c/kWh` or `NZD/kWh`, per subentry; the spec also mentions $/MWh. Convert all three by reading `unit_of_measurement`.
- Settled sensor (`*_settled_price`) attributes: `trading_period`, `timestamp` (the settled period), `node`, `history[]`. Its state lags one period.
- Live sensor attributes: `timestamp`, `trading_period`, `node`, `schedule`.
- Day-ahead/intraday sensors have `forecast[]` and `history[]` of `{period_start, trading_period, price}`. Prices are in the sensor's unit.
- Device class is `MONETARY`, with no state_class.

## Step 0: setup

Work on a branch from `main`, then run `./scripts/setup` (`uv sync`). Remove the placeholder `Status` sensor and its test when the real sensors land.

## Step 1: pure tariff engine (no HA imports)

The engine is pure so the unit tests, the `price_at` service and, later, the backtest can all share it.

`custom_components/electricity_billing_nz/tariff.py`
- Frozen dataclasses:
  - `BandRule(months, weekdays|None, holidays: bool|None, windows: list[(time, time)])`
  - `BandSchedule(rules_by_band, default)`
  - `NetworkVersion(effective_from, source, daily, import_rates: dict[band, float], export_credit: dict[band, float], bands: BandSchedule)`
  - `LossVersion(effective_from, source, factor)`
  - `RetailerVersion(effective_from, import_margin, export_margin, transmission, metering_daily, retailer_daily, levy, export_floor|None)`. Transmission belongs here, not in `NetworkVersion` (see Step 2).
- `version_at(versions, at)`: the latest `effective_from <= at`, where `effective_from` is a date taken as 00:00 Pacific/Auckland.
- `resolve_band(schedule, period_start)` → band name. Rules are evaluated in NZ local time on the **period start**, using `holidays.country_holidays("NZ")` when a rule needs it.
- `next_band_change(schedule, after)` → datetime. Walk half-hour steps up to 8 days, so DST days stay correct.
- `PriceBreakdown` dataclass: `spot, margin, loss_factor, network, transmission, levy, export_credit, gst, tou_band, network_version, retailer_version`, plus `.price`.
- `import_price(spot, period_start, tariff, gst)` and `export_price(...)` implement the spec's formulas exactly:
  - `P_imp = ((S + m_imp)·L + N_imp + T + V)·G`
  - `P_exp = max(floor, S − m_exp + C_exp)·G`. The floor is applied ex-GST, and there is none by default.
  - If a network, loss or retailer version doesn't cover the period, they raise `NoRatesError` and the sensors are unavailable. A missing component never counts as 0, which would give a too-low price.
- `daily_charge(at, tariff, gst)`.
- `to_nzd_per_kwh(value, unit)` handles c/kWh, NZD/kWh and $/MWh.
- `Tariff` aggregates the network, loss and retailer version lists.

## Step 2: bundled network data

- `custom_components/electricity_billing_nz/networks/vector.yaml`, transcribed from Vector's [Pricing Schedule and Policy v2026.1](https://media.vector.co.nz/production/documents/electricitypricescheduleseffectivefrom1april2026.pdf) (effective 1 April 2026) and [v2025.1](https://media.vector.co.nz/production/documents/electricitypricescheduleseffectivefrom1april2025.pdf) (effective 1 April 2025). All prices ex GST, $/kWh and $/day. The 2026 version:

  | Category | Daily (FIXD) | Off-peak (OFPK) | Peak (PEAK) | Injection off-peak (IJOP) | Injection peak (IJPK) |
  | --- | --- | --- | --- | --- | --- |
  | ARHLC | 0.9000 | 0.0457 | 0.1504 | 0.0000 | (0.0524) |
  | ARHLU | 0.9000 | 0.0466 | 0.1513 | 0.0000 | (0.0524) |

  - Loss category VECA1 is 1.0476 (4.54%) with respect to the customer meter.
  - From 2026-04-01 the bands are `vector_tou_2026`, as in the spec. Peak applies Monday to Sunday including public holidays; the hours are only shown as a chart in the PDF. The 2025 version uses `vector_tou_2025` (below).
  - ARHLU is included so the user can switch; in 2026 it costs 0.09 c/kWh more in both bands (0.10 c in 2025).
  - Each version carries `source:` (its PDF URL) and its `schedule_version` (v2025.1 or v2026.1), so every number can be traced.
  - A 2025-04-01 version (v2025.1) lets replays of earlier periods (the backtest) price correctly. It differs from 2026 in more than the rates:
    - Daily $0.75. ARHLC off-peak 0.0444 / winter peak 0.1575; ARHLU 0.0454 / 0.1585. VECA1 1.0533. Injection credit 0.
    - Peak is Monday–Friday (including public holidays) 07:00–11:00 and 17:00–21:00, all year; weekends are off-peak.
    - Vector prices peak by season: October–March "summer peak" costs exactly the off-peak rate, so `vector_tou_2025` treats it as off-peak (same prices, and the peak sensor only flags periods that cost more). Peak is April–September weekdays.
  - Before 2025-04-01 there's no data, so prices raise `NoRatesError`.
- **Spec correction: transmission is not network data.** Vector doesn't charge consumers transmission per kWh. It invoices retailers monthly per GXP share (ROS, Mount Roskill: $17.1256 per 1/1000% share). The 3.02 c/kWh on the bill is Ecotricity's own per-kWh recovery of that charge, so `transmission` moves to the retailer plan. Update the spec's component table and symbol table, which list it under the network price category.
- `networks.py`: `async_load_networks(hass)` reads every `networks/*.yaml` via `hass.async_add_executor_job` + `yaml.safe_load` (PyYAML ships with HA). It validates and converts into the Step 1 dataclasses, and caches the result in `hass.data[DOMAIN]`. It also exposes `list_distributors()`, `categories(distributor)` and `loss_categories(distributor)`.

## Step 3: config entry data model and flows

`const.py` adds CONF_* keys. Entry layout (VERSION 1.2; 1.1 used the settled sensor as unique_id):
- `data`:
  - `name` (ICP label)
  - `icp?` (the ICP number; also the unique_id when set)
  - `settled_entity`, `live_entity?`, `forecast_entity?`
  - `distributor`, `price_category`, `loss_category`
  - `gst_inclusive`
- `options`:
  - `retailer_versions: [ {effective_from, ...} ]`
  - `network_overrides: [ {effective_from, field: value…} ]`
  - `billing_cycle_day` (Phase 2)

The effective tariff is the bundled versions merged with the overrides. Use the same `version_at` lookup; an override version replaces fields from its date onwards.

`config_flow.py` replaces the name-only scaffold. It keeps the `probatio` schema style the template uses.
1. `user`: name, plus EntitySelector(domain=sensor, integration=`electricityinfo`) for the settled, live and forecast entities. Validate the settled entity's unit. Sources are stored by entity registry id so they follow renames (entity id if unregistered). An optional ICP number (10 digits, 2 letters, 3 check characters) is the unique_id, so a connection can't be added twice; without one there's no unique_id, because one node sensor can feed several entries (a second ICP on the node, or a plan comparison). The options menu can set or clear the ICP.
2. `network`: distributor select, then price category and loss category selects built from the bundled data, plus a `custom` option for a manual-entry step.
   With an ICP, the flow reads the record from the Electricity Authority's public "Your meter" page (`/your-power/your-meter/icp/?icp=…&load_more=True`; there's no keyless API). It pre-selects the network whose `participants` list has the record's network participant code (e.g. `VECT`), and the price and loss categories when the bundled data has them. If the record's POC isn't the settled sensor's `node`, the network step shows a warning that doesn't block. A failed lookup leaves the selects empty.
   Then `network_rates` shows the bundled values in force today (the first schedule if the data hasn't started), keyed and entered like a network override. Values changed from the bundled ones are stored as one override from that schedule's start (the later of the network and loss versions).
3. `retailer`: plan name, import margin, export margin, transmission, metering daily, retailer daily, levy and export floor. Entered in c/kWh and $/day for usability, stored in NZD.
4. `gst`: incl (default) or ex.

`OptionsFlowWithReload` menu:
- `add_rate_version`: network or retailer, an effective-from date (DateSelector), then the changed fields. Earlier versions are shown read-only.
- `override_network`
- `change_sources`

Update `strings.json` and `translations/en.json` together; `tests/test_translations.py` already checks they match.

## Step 4: runtime engine (`engine.py`)

`BillingEngine`, stored in `entry.runtime_data` (extend `RuntimeData` in `__init__.py`):
- Builds the `Tariff` from the bundled data and the entry options.
- Subscribes with `async_track_state_change_event` to the source entities.
- Schedules `async_track_point_in_time` for the next band boundary and the next rate-version effective time. It reschedules after each fire, so there's no polling.
- On every trigger it recomputes and sends `async_dispatcher_send(f"{DOMAIN}_{entry_id}_update")`.
- Computes from source state:
  - **Settled**: spot from the settled state. Band and versions come from the **settled period's start**, taken from the `timestamp` attribute; check that it is the period start, and if not, derive it from `trading_period` and the date. This is spec rule 1, and it also fixes the template bug that priced the settled N−1 value using now's peak flag.
  - **Live**: spot from the live state, priced for the current period start (now floored to :00/:30 NZ).
  - **Forecast**: each `forecast[]` row priced at its `period_start`, or the period before when its `trading_period` says the stamp is the period end → list `{period_start, trading_period, price, tou_band}`. State = the current period, otherwise the next one.
- Sets the price to unavailable when the spot is unavailable or non-numeric. There is no carry-forward.

## Step 5: platforms

`sensor.py`: one `DeviceInfo` per entry (named after the ICP label, `entry_type=SERVICE`). `_attr_has_entity_name` and translation keys.

| Entity | Notes |
| --- | --- |
| `import_price`, `export_price` | settled; `NZD/kWh`, `state_class=measurement`, no device_class (matches the templates and the Energy dashboard) |
| `import_price_live`, `export_price_live` | from the live source; created only if it's configured |
| `import_price_forecast`, `export_price_forecast` | `forecast` attribute; created only if a forecast source is configured |
| `daily_charge` | `NZD/day` |
| `tou_band` | `SensorDeviceClass.ENUM`, options from the schedule |
| `next_band_change` | `SensorDeviceClass.TIMESTAMP` |

- Price sensors subclass `RestoreSensor` and restore the state and attributes on startup.
- Attributes are the `PriceBreakdown` fields (ex GST) plus `trading_period`.
- Precision: 4 dp, as the templates use.

`binary_sensor.py`: `peak` (on when the band is `peak`). Add `Platform.BINARY_SENSOR` to `PLATFORMS`.

## Step 6: services (`services.py`, registered in `async_setup`)

- `price_at(config_entry_id, timestamp, spot, spot_unit="NZD/kWh")` uses `SupportsResponse.ONLY` and returns `{import_price, export_price, breakdown}`. It uses the versions in force at that time.
- `export_versions` (planned for Phase 2, shipped early) returns the merged versions as a YAML string.
- Add `services.yaml` and service strings.

## Step 7: tests (pytest-homeassistant-custom-component; extend `tests/conftest.py`)

- `test_tariff.py`: a parametrised table of `(period_start, spot) → expected import/export`, hand-computed from the spec's constants with the margin. Covers:
  - Jun 07:00 and 17:00 peak, May 07:00 off-peak, and Oct–Apr with no credit
  - the 16:30 vs 17:00 period and 21:30 vs 22:00
  - DST ending 2026-04-05 and starting 2026-09-27
  - the 1 April version switch at 00:00 NZ, using a second fake version
  - negative spot, the export floor, and GST 1.0
- `test_networks.py`: every bundled YAML loads and validates. Each category has rates for every band its schedule can return.
- `test_template_parity.py`: a fuzzed and edge-case comparison against a direct Python port of the margin-aware template formula. It should agree to 1e-4 when the band comes from the same instant.
- `test_config_flow.py`: the full user flow, the custom-network path, an invalid unit, an abort on duplicates, and the options flow adding a future version.
- `test_sensor.py` / `test_binary_sensor.py`:
  - Set source states with `hass.states.async_set`, using the upstream attribute shapes above.
  - Use `freezer` to cross a band boundary and a future version date without reloading (an acceptance item).
  - Spot unavailable → unavailable.
  - c/kWh and NZD/kWh sources.
- `test_services.py`: `price_at` responses.

## Step 8: docs and cutover (Phase 1 exit)

- README: drop the "Status: in development" note and move the shipped items out of "Planned features". Add configuration steps after the existing Marketplace install section. Keep the wording to "marketplace", not HACS; `hacs.json` and the HACS validation in `validate.yml` stay.
- In `electricity-pricing`, as a separate PR in that repo:
  - Point `electricity_import_price` / `export_price` / `_live`, `electricity_fixed_daily_charge` and `vector_peak_period` consumers at the new entities. Or keep the old entity ids as thin template aliases for one release so history and dashboards keep working.
  - Rewrite the overnight-window and peak-export forecast templates and the `emhass.yaml` cost/prod forecasts to read the new `forecast` attribute.
  - Have `backtest.py` call `price_at` or import `tariff.py`.
  - Then delete the duplicated constants.

## Phase 2: billing cycle

Goal: estimate the bill for the current billing cycle, line by line, so it can be checked against the invoice. This replaces these parts of `electricity_pricing.yaml`:
- the `grid_import_billing` / `grid_export_billing` utility meters (split by band) and the automation that switches their tariff
- the `electricity_import_cost_total` / `electricity_export_earnings_total` accumulators and their `*_billing` meters
- the `billing_cycle_import_energy_rate` / `_export_energy_rate` templates

`export_versions` was planned here but shipped in Phase 1.

### Before starting: the template package on ha-dev, from a switch-over branch

The parity checks for Phase 1 (Verification 3) and Phase 2 (Step 6) need the template package running on ha-dev next to the integration.

1. **Branch.** `integration-switch-over` in `electricity-pricing`, cut from `ecotricity-margin` once its margin edits were committed (they are the parity reference).
2. **Phase 1 switch-over on that branch (Step 8, first part).** `electricity_import_price`, `_export_price`, `_import_price_live`, `electricity_fixed_daily_charge` and `vector_peak_period` keep their unique ids and become aliases of the integration's entities (config entry "Home", so `sensor.home_*`). That way history, the dashboard, the accumulators, the billing meters and EMHASS keep working. The old formulas stay as `*_template` sensors for the comparison, to be deleted after it.
3. **Install on ha-dev.** It goes in `/config/packages/electricity_pricing.yaml`: ha-dev loads `packages/`, while the Pi loads `integrations/`. Check the config, then do a full restart; utility meters don't load on a template reload. Don't install `emhass.yaml` on ha-dev.
4. **Entity ids differ on ha-dev.** The Alpha ESS entities are `*.alpha_ess_energy_statistics_al2002118050331_al2002118050331_*` instead of `*.al2002118050331_*`; the package builds `sensor.grid_import_energy` / `_export_energy` from them itself. ha-dev has no charge-time entities and no Solcast, Helios or Forecast.Solar, so the charge-window and solar-forecast templates fall back or stay unavailable there.
5. **Keep ha-dev away from the inverter.** ha-dev's Alpha ESS integration reaches the real inverter (`bathighcap`, the charge-schedule buttons).
   - `tools/deploy-dev.sh` in `electricity-pricing` drops the `battery_*` automations (ruamel.yaml, keeping comments) and maps the ids from step 4.
   - It refuses to copy if any `number.set_value`, `switch.turn_on` / `turn_off` or `time.set_value` is left.
   - It then copies the file over `ssh ha-dev`, checks the config through the API and, with `--restart`, restarts.
6. **Margins on ha-dev.** The "Home" entry has 0 import and export margins. For parity it needs a retailer version with +2.86 c import and −1.90 c export.

### Options (no migration; billing is off until an import energy entity is set)

- `import_energy_entity` and `export_energy_entity?`: sensors with an energy unit and `state_class` `total` or `total_increasing`. Store them by registry id, like the price sources.
- `billing_cycle_day`: 1–28, default 1. A cycle runs from local midnight on that day to the same day next month. Days 29–31 are left out because short months have no such day.
- New options menu item `billing`. The config flow doesn't ask for these, to keep setup short.

### Metering and pricing (`billing.py`, no HA imports)

The template accumulators multiply each kWh by the settled price shown at that moment. That price lags one period and ignores band changes. Instead, give each trading period its own energy and price it at that period's settled spot:
- **Readings to periods.**
  - Each energy state change adds `new − last_reading` to the trading periods since the previous reading, spread in proportion to time. A gap after a restart is spread over the periods it covers rather than landing in one.
  - `last_reading` is restored across restarts, as in the template.
  - A falling reading counts as a meter reset, so the increment is `new`.
  - Unavailable states are skipped, and the next good reading catches up.
- **Pricing.**
  - A period's energy is priced when the settled source reports that period: from the state when its `timestamp` matches, otherwise from its `history[]`.
  - Each pricing goes through `price_import` / `price_export` with the versions in force at the period start, so a version that changes mid-cycle is handled.
  - Periods still waiting for a price are counted at the live price for that period (stored when it arrived) and flagged as provisional.
  - After 48 h without a settled price, the provisional price becomes final and the period is counted in `unsettled_periods`.
- **Cycle totals, ex GST, per band.**
  - Import: kWh, kWh with losses, energy (`(spot + margin) × loss`), network, transmission and levy.
  - Export: kWh, spot plus margin (floored), and the network export credit.
  - Daily charges are added per day of the cycle, each at the rate in force that day. GST is applied once, at the end.
- **Rollover.** At the cycle start, the totals move to `previous_cycle`. Periods from the old cycle that are still pending are priced into `previous_cycle` when they settle.
- **DST.** A 23 h or 25 h day has 46 or 50 periods, so periods are keyed by their UTC start.

### Persistence

One `helpers.storage.Store` per entry (`electricity_billing_nz.<entry_id>`) holds:
- the cycle start
- the totals and `previous_cycle`
- the pending periods, with their energy and provisional price
- the last readings
- the lifetime totals

It is saved with `async_delay_save` (30 s) and on stop, and removed in `async_remove_entry`.

### Engine

`BillingEngine` gets a `BillingMeter`, created only when billing is configured. It:
- subscribes to the energy entities
- hands each newly settled period (`period_start`, spot) to the meter
- schedules a timer for the cycle start
- sends the existing update signal

### Sensors (created only when billing is configured)

| Entity | Unit / class | Notes |
| --- | --- | --- |
| `billing_cycle_import_energy`, `billing_cycle_export_energy` | kWh, `total`, `last_reset` = cycle start | attributes: kWh per band, per invoice line |
| `billing_cycle_import_energy_rate` | NZD/kWh ex GST | energy ÷ kWh with losses, same definition as the template |
| `billing_cycle_export_energy_rate` | NZD/kWh ex GST | export earnings without the network credit ÷ kWh, same definition as the template |
| `billing_cycle_estimated_total` | NZD, per `gst_inclusive` | attributes are the invoice lines: days × each daily charge, kWh × rate per band for network and energy, levy, export, GST, plus `provisional_periods`, `unsettled_periods`, `cycle_start`, `cycle_end` and `previous_cycle` |
| `import_cost_total`, `export_earnings_total` | NZD, `total` (it can fall when prices are negative) | lifetime totals that replace the template accumulators; the daily meters in `electricity_pricing.yaml` can point at them |

### Not in Phase 2

- Backfill. A cycle that starts after billing is set up is partial, and `billing_cycle_estimated_total` says so with `complete: false`. Rebuilding it from recorder statistics is possible but would need the settled history for the whole cycle.
- Final prices. Invoices use the final prices, published days later; the settled source gives interim ones. Expect small differences, and note them in the README.

### Steps

1. `billing.py` and `tests/test_billing.py`. Cover:
   - spreading readings across periods, meter resets and unavailable readings
   - provisional, then settled, then the 48 h fallback
   - a version change mid-cycle, rollover with late settlements, and DST days
   - the cycle day landing on the 28th
2. Store and engine wiring. Test restarts with pending periods and the cycle timer.
3. The `billing` options step, `strings.json` and `translations/en.json`.
4. The sensors, then README and this plan.
5. Cutover PR in `electricity-pricing`: remove the parts listed above and point the daily meters and the dashboard at the new entities.
6. Verification: run it next to the templates for one billing cycle, then compare `billing_cycle_estimated_total`'s lines with the invoice. Expect differences only from final versus interim prices.

## Later phases (outline only)

- **Phase 3:** more `networks/*.yaml` (Orion, Wellington Electricity, Powerco, Unison), using weekday and holiday band rules, then the marketplace release.

## Keeping network data current (semi-automatic)

The integration stays offline at runtime (`iot_class: calculated`). A scheduled repo job finds new schedules and drafts the change; a person reviews it before release.

What's available:
- **Vector:** a PDF only, with no CSV, XLSX or API. The pricing page links it as `media.vector.co.nz/production/documents/electricitypricescheduleseffectivefrom1april{YYYY}.pdf`, and the PDF starts with `Version: v{YYYY}.N` / `Effective: 1 April {YYYY}`.
  - `pdftotext -layout` extracts the rate tables and loss factors as fixed-width text.
  - The TOU hours are in a chart, and also in the price component definitions as text (`-PEAK 17:00 to 22:00 for months May to September`).
  - Vector files no EIEP12 notifications on EMI.
- **Other distributors:** the EMI dataset `Retail/Datasets/DistributorPriceChangeNotifications/{YEAR}` has EIEP12 CSV/TXT files from about 20 distributors (Orion `ORON`, Wellington `WATA`/`ELEC`, …).
  - These files have rates per category, time-block code and effective date.
  - They have no clock windows or loss factors, and the layouts vary.
- **Category codes:** EMI `MarketStructure/DistributionPriceCategoryCodes` is a daily CSV of NSP, price category code, effective date and ICP counts. It has codes, not rates, so it can only confirm that a configured category still exists.

Vector (built):
1. `tools/vector_schedule.py` finds every schedule linked from Vector's pricing page that's newer than the bundled data, and adds them oldest first, stopping at the first it can't read. Each is extracted with `pdftotext -layout`. It also re-reads the bundled schedule's PDF and flags it when its `Version:` has changed (a corrected PDF).
   - It reads the ARHLC/ARHLU rows by header column position (FIXD, OFPK, PEAK, IJOP, IJPK; brackets mean negative, so an injection charge in brackets is an export credit), the VECA1 loss factor, the `-PEAK … for months …` definitions (any other wording is an error), the `Version:` line (the YAML's `schedule_version`) and the `Effective:` date, which must match the link's.
   - Peak must apply Monday to Sunday. Any other layout, such as 2025's separate summer and winter PEAK columns, raises `LayoutError` instead of guessing.
   - It appends the version to `vector.yaml` with ruamel.yaml, keeping the comments, and reuses the latest band schedule when the peak rules are unchanged. Changed rules get a new band schedule; an existing one is never overwritten.
2. `.github/workflows/vector-schedule.yml` runs it monthly (and by hand). For a new schedule that parses, it runs the tests and opens a PR for a person to check against the PDF. One that doesn't parse gets an issue to transcribe by hand.
3. `tests/test_vector_schedule.py` checks the parser reproduces the bundled 2026 data from a saved `pdftotext` fixture, rejects the 2025 layout, and writes a simulated 2027 schedule.

Later (Phase 3): the EIEP12 distributors. List the EMI folder for new `{DIST}_E_*_PRICE_{YYYY}04_*` files, parse the `DET` rows and write a draft version the same way.

Retailer values are not covered by any of this; the user enters them through the options flow ("add rate version"). These are the margin, transmission, levy and daily charges. "Override network value" covers network changes that land before a release.

## Critical files

- `custom_components/electricity_billing_nz/`: `tariff.py`, `networks.py`, `networks/vector.yaml`, `engine.py`, `config_flow.py`, `const.py`, `__init__.py`, `sensor.py`, `binary_sensor.py`, `services.py`, `services.yaml`, `strings.json`, `translations/en.json`
- `tests/`: the files listed in Step 7
- Reference only:
  - ha-electricityinfo-nz `custom_components/electricityinfo/sensor.py`: source attribute shapes
  - electricity-pricing `packages/electricity_pricing.yaml` (the `ecotricity-margin` working copy): parity formulas

## Verification

1. `./scripts/lint`, `uv run --group dev mypy custom_components`, `uv run --group dev pytest`. All green, matching CI.
2. `./scripts/develop` brings up a dev HA with `config/`. Add stub `sensor.ros0221_*` states, or point at a running ha-electricityinfo-nz. Run the config flow, then check the entities, attributes and the `price_at` response in Developer Tools.
3. Parity on the live system: install it next to the existing templates for a few days and compare `sensor.<name>_import_price` with `sensor.electricity_import_price` (the margin version) in history. Expect exact matches, except for the period just after each peak boundary, where the new band-from-period-start rule is intentionally correct.
4. Replay October 2026 using InfluxDB settled prices and grid flow (the `export_influx.py` output) through `price_at`, and compare with the template accumulators.
5. Energy dashboard: select the new import/export price entities and confirm HA accepts them.
6. EMHASS: feed the forecast-derived `load_cost_forecast` / `prod_price_forecast`, and compare the optimisation with the current template payload.
