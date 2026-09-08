"""The one display zone: `CCDASH_TZ` read once, every date decision routed
through here.

The sole reader of `store.tz`, which `main` rebinds at startup (a `ZoneInfo`
or `None`). `None` means UTC, so an unset `CCDASH_TZ` and an explicit UTC read
the same. Day-bucketing and display both pass through here, so the backend and
the frontend agree on where a day begins.
"""

import datetime
import os
import sys
import zoneinfo

from . import store


def from_env() -> zoneinfo.ZoneInfo | None:
    """The zone `CCDASH_TZ` names, or None for UTC.

    Unset or empty reads as None (UTC). An unknown IANA name warns on stderr
    and falls back to None rather than crashing: a dashboard that refuses to
    boot over a typo is worse than one showing UTC.
    """
    name = os.environ.get("CCDASH_TZ", "").strip()
    if not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        sys.stderr.write("ccdash: unknown CCDASH_TZ %r, using UTC\n" % name)
        return None


def _zone() -> datetime.tzinfo:
    """The configured zone, UTC when none is set."""
    return store.tz or datetime.timezone.utc


def to_zone(epoch: int) -> datetime.datetime:
    """UTC Unix seconds as an aware datetime in the configured zone.

    Day-bucketing reads `.date()` / `.weekday()` / `.hour` off the result, so
    the day it lands in follows `CCDASH_TZ`.
    """
    return datetime.datetime.fromtimestamp(epoch, _zone())


def date_to_epoch(d: str) -> int:
    """A `YYYY-MM-DD` date at midnight in the configured zone, as UTC seconds.

    The seam #10 calls to turn a caller-supplied `start_date` / `end_date` into
    the UTC bound a query compares against.
    """
    midnight = datetime.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=_zone())
    return int(midnight.timestamp())


def zone_name() -> str:
    """The configured IANA name, or "UTC" when none is set. Read by
    `api_filters` so the frontend renders in the same zone the backend buckets
    on."""
    return store.tz.key if store.tz else "UTC"
