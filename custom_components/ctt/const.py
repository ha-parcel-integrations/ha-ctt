"""Constants for the CTT parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "ctt"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. Each CAPABILITIES_BY_VARIANT
# value must be a subset of this — it exists so a typo there fails a test
# instead of silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# CTT's payload does not expose weight, dimensions or a delivery window to an
# anonymous caller: ``DeliveryDeadlineEnd`` and ``DeliveryTimeSlot`` exist as
# fields but came back empty on all four real parcels captured 2026-09-06.
# Claiming ``delivery_window`` on that evidence would put a wrong column on
# the docs site — revisit if a future parcel ever populates them.
# The variant is picked per parcel by the tracking code's shape, not by a setting.
CAPABILITIES_BY_VARIANT = {
    "CTT": frozenset({"pickup_point", "url", "history"}),
    "CTT Express": frozenset({"delivery_window", "pickup_point", "url", "history"}),
}

# ``TRACKING_API_URL`` is CTT's OutSystems data action — POST, not GET, the
# tracking code goes in the request body (not this URL), and the full session
# bootstrap / version-token mechanics live in ctt.py, the one module that
# actually calls it. It always answers ``200`` — including for an unknown
# code — so ``Found: false`` inside the body is the semantic-404, and (this is
# the trap) it is *also* what a CTT-side outage looks like; ctt.py's
# maintenance check is what tells the two apart.
#
# ``TRACKING_URL`` deliberately points at the legacy ``www.ctt.pt`` page
# rather than the OutSystems detail screen it renders: it is the address a
# user recognises, it survives ``appserver`` host changes, and it is the one
# CTT itself links from its site.
TRACKING_API_URL = (
    "https://appserver.ctt.pt/CustomerArea/screenservices/CustomerArea"
    "/CustomerArea/PublicArea_Detail/DataActionGetObjectEventsByInputObjectCode"
)
TRACKING_URL = (
    "https://www.ctt.pt/feapl_2/app/open/objectSearch/objectSearch.jspx"
    "?objects={tracking_code}"
)

EXPRESS_TRACKING_API_URL = "https://wct.cttexpress.com/p_track_redis_v2.php"
EXPRESS_TRACKING_URL = (
    "https://www.cttexpress.com/localizador-de-envios/?sc={tracking_code}"
)

# A plain aiohttp UA is refused by Cloudflare (error 1010) before it ever
# reaches OutSystems — this string only needs to look like a browser, not be
# any particular one.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class CTTApiError(Exception):
    """Raised when a CTT API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"CTT API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this carrier has no account or parcel feed, so the
# user enters the codes themselves. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see scaffold/CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight (registered, in_transit, at_pickup_point, unknown, problem,
# returning).
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default even when — as here — the timeline arrives in
# the same response and costs no extra request: it is a large attribute, and on
# carriers that need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
