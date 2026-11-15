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
  - A missing component counts as 0.
- `daily_charge(at, tariff, gst)`.
- `to_nzd_per_kwh(value, unit)` handles c/kWh, NZD/kWh and $/MWh.
- `Tariff` aggregates the network, loss and retailer version lists.

## Step 2: bundled network data

- `custom_components/electricity_billing_nz/networks/vector.yaml`, transcribed from Vector's [Pricing Schedule and Policy v2026.1](https://media.vector.co.nz/production/documents/electricitypricescheduleseffectivefrom1april2026.pdf) (effective 1 April 2026, all prices ex GST, $/kWh and $/day):

  | Category | Daily (FIXD) | Off-peak (OFPK) | Peak (PEAK) | Injection off-peak (IJOP) | Injection peak (IJPK) |
  | --- | --- | --- | --- | --- | --- |
  | ARHLC | 0.9000 | 0.0457 | 0.1504 | 0.0000 | (0.0524) |
  | ARHLU | 0.9000 | 0.0466 | 0.1513 | 0.0000 | (0.0524) |

  - Loss category VECA1 is 1.0476 (4.54%) with respect to the customer meter.
  - Bands are `vector_tou_2026`, as in the spec. Peak applies Monday to Sunday including public holidays; the hours are only shown as a chart in the PDF.
  - ARHLU is included so the user can switch; it costs 0.09 c/kWh more in both bands.
  - Each version carries `source:` (the PDF URL) and `schedule_version: v2026.1`, so every number can be traced.
  - Add a 2025-04-01 version with zero injection credit, so replays of earlier periods (the backtest) are correct. Injection prices were zero before 1 April 2026.
- **Spec correction: transmission is not network data.** Vector doesn't charge consumers transmission per kWh. It invoices retailers monthly per GXP share (ROS, Mount Roskill: $17.1256 per 1/1000% share). The 3.02 c/kWh on the bill is Ecotricity's own per-kWh recovery of that charge, so `transmission` moves to the retailer plan. Update the spec's component table and symbol table, which list it under the network price category.
- `networks.py`: `async_load_networks(hass)` reads every `networks/*.yaml` via `hass.async_add_executor_job` + `yaml.safe_load` (PyYAML ships with HA). It validates and converts into the Step 1 dataclasses, and caches the result in `hass.data[DOMAIN]`. It also exposes `list_distributors()`, `categories(distributor)` and `loss_categories(distributor)`.

## Step 3: config entry data model and flows

`const.py` adds CONF_* keys. Entry layout (VERSION 1):
- `data`:
  - `name` (ICP label)
  - `settled_entity`, `live_entity?`, `forecast_entity?`
  - `distributor`, `price_category`, `loss_category`
  - `gst_inclusive`
- `options`:
  - `retailer_versions: [ {effective_from, ...} ]`
  - `network_overrides: [ {effective_from, field: value…} ]`
  - `billing_cycle_day` (Phase 2)

The effective tariff is the bundled versions merged with the overrides. Use the same `version_at` lookup; an override version replaces fields from its date onwards.

`config_flow.py` replaces the name-only scaffold. It keeps the `probatio` schema style the template uses.
1. `user`: name, plus EntitySelector(domain=sensor, integration=`electricityinfo`) for the settled, live and forecast entities. Validate the settled entity's unit. unique_id = settled entity id.
2. `network`: distributor select, then price category and loss category selects built from the bundled data, plus a `custom` option for a manual-entry step.
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
  - **Forecast**: each `forecast[]` row priced at its `period_start` → list `{period_start, trading_period, price, tou_band}`. State = the current period, otherwise the next one.
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
- `export_versions` is **Phase 2**, but cheap. It returns the merged versions as a YAML string.
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

## Later phases (outline only)

- **Phase 2:**
  - `billing_cycle_import_energy_rate` / `_export_energy_rate` and `billing_cycle_estimated_total`, moved from `electricity_pricing.yaml`. They need energy-entity options, or internal utility meters split by band.
  - The `billing_cycle_day` option.
  - `export_versions`.
- **Phase 3:** more `networks/*.yaml` (Orion, Wellington Electricity, Powerco, Unison), using weekday and holiday band rules, then the marketplace release.

## Keeping network data current (semi-automatic)

The integration stays offline at runtime (`iot_class: calculated`). A scheduled repo job finds new schedules and drafts the change; a person reviews it before release.

What's available:
- **Vector:** a PDF only, with no CSV, XLSX or API. The pricing page links it as `media.vector.co.nz/production/documents/electricitypricescheduleseffectivefrom1april{YYYY}.pdf`, and the PDF starts with `Version: v{YYYY}.N` / `Effective: 1 April {YYYY}`.
  - `pdftotext -layout` extracts the rate tables and loss factors as fixed-width text.
  - The TOU hours appear only as a chart.
  - Vector files no EIEP12 notifications on EMI.
- **Other distributors:** the EMI dataset `Retail/Datasets/DistributorPriceChangeNotifications/{YEAR}` has EIEP12 CSV/TXT files from about 20 distributors (Orion `ORON`, Wellington `WATA`/`ELEC`, …).
  - These files have rates per category, time-block code and effective date.
  - They have no clock windows or loss factors, and the layouts vary.
- **Category codes:** EMI `MarketStructure/DistributionPriceCategoryCodes` is a daily CSV of NSP, price category code, effective date and ICP counts. It has codes, not rates, so it can only confirm that a configured category still exists.

Plan (Phase 3; Vector part can come earlier):
1. `scripts/check_network_updates.py` (dev-only, run with `uv run`). For each `networks/*.yaml` it looks for the next year's source:
   - **Vector:**
     - Request the expected PDF URL. If that misses, scan the pricing page for a link matching `electricitypricescheduleseffectivefrom`.
     - Extract the text and parse the residential tables by header column positions (FIXD, OFPK, PEAK, IJOP, IJPK) and the loss-category table. Brackets mean negative.
     - Check the `Version`/`Effective` lines.
   - **EIEP12 distributors:** list the EMI folder for new `{DIST}_E_*_PRICE_{YYYY}04_*` files and parse the `DET` rows.
   - Either way, write a draft version into the YAML and print the diff.
2. `.github/workflows/network-updates.yml` runs the script weekly from January to April and opens a draft PR when something new appears. A reviewer confirms the TOU hours against the PDF chart before merging.
3. A test checks that the parser reproduces the 2026 Vector rows from a saved text fixture of the v2026.1 PDF.
4. Retailer values are not covered by any of this; the user enters them through the options flow ("add rate version"). These are the margin, transmission, levy and daily charges. "Override network value" covers network changes that land before a release.

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
