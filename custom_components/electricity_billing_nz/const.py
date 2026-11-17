"""Constants for electricity_billing_nz."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "electricity_billing_nz"

# Config entry data
CONF_SETTLED_ENTITY = "settled_entity"
CONF_LIVE_ENTITY = "live_entity"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_DISTRIBUTOR = "distributor"
CONF_PRICE_CATEGORY = "price_category"
CONF_LOSS_CATEGORY = "loss_category"
CONF_GST_INCLUSIVE = "gst_inclusive"
# Optional ICP identifier; when set it is the entry's unique id, so the same
# connection can't be added twice. Entries without one (e.g. to compare
# plans on the same node) have no unique id.
CONF_ICP = "icp"

# Config entry options
CONF_RETAILER_VERSIONS = "retailer_versions"
CONF_NETWORK_OVERRIDES = "network_overrides"
# Billing cycle: off until an import energy sensor is set.
CONF_IMPORT_ENERGY_ENTITY = "import_energy_entity"
CONF_EXPORT_ENERGY_ENTITY = "export_energy_entity"
CONF_BILLING_CYCLE_DAY = "billing_cycle_day"
DEFAULT_BILLING_CYCLE_DAY = 1
# Every month has a 28th.
MAX_BILLING_CYCLE_DAY = 28

# Retailer plan fields. Rates are stored in NZD/kWh and NZD/day; the forms
# take c/kWh for per-kWh values.
CONF_EFFECTIVE_FROM = "effective_from"
CONF_PLAN_NAME = "plan_name"
CONF_IMPORT_MARGIN = "import_margin"
CONF_EXPORT_MARGIN = "export_margin"
CONF_TRANSMISSION = "transmission"
CONF_LEVY = "levy"
CONF_METERING_DAILY = "metering_daily"
CONF_RETAILER_DAILY = "retailer_daily"
CONF_EXPORT_FLOOR = "export_floor"

RETAILER_PER_KWH_FIELDS = (
    CONF_IMPORT_MARGIN,
    CONF_EXPORT_MARGIN,
    CONF_TRANSMISSION,
    CONF_LEVY,
)
RETAILER_DAILY_FIELDS = (CONF_METERING_DAILY, CONF_RETAILER_DAILY)

# The first retailer version applies to all history.
FIRST_EFFECTIVE_FROM = "2000-01-01"

PRICE_UNIT = "NZD/kWh"
# Decimal places for prices in states, attributes and service responses.
PRICE_PRECISION = 6
DAILY_UNIT = "NZD/day"
CURRENCY = "NZD"

SERVICE_PRICE_AT = "price_at"
SERVICE_EXPORT_VERSIONS = "export_versions"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TIMESTAMP = "timestamp"
ATTR_SPOT = "spot"
ATTR_SPOT_UNIT = "spot_unit"
