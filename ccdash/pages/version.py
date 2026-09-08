"""The running version and the once-a-day check for a newer release.

The check runs off a daemon thread spawned in `main()`: it asks GitHub for the
latest release, compares it against `__version__`, and holds the answer in
module state that `api_version` reads -- no GitHub call per request. It never
blocks a request and never raises into one: a network hiccup or an unparsable
tag resolves to "no update", silently. `CCDASH_ENABLE_UPDATE_CHECK` turns the
whole thing off, and with it the only outbound call ccdash makes.
"""

import json
import os
import threading
import time
import urllib.request
from typing import Any

from .. import __version__

LATEST_RELEASE_API = (
    "https://api.github.com/repos/dumontchristophe/ccdash/releases/latest"
)
RELEASE_TAG_URL = "https://github.com/dumontchristophe/ccdash/releases/tag/"

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 10

# The tag a check found newer than __version__, None until then and again after
# a check that failed or found nothing newer. The release page is derived from
# it, so there is one value to hold, not two. Guarded by _lock: the check thread
# writes it, request threads read it.
_lock = threading.Lock()
_latest: str | None = None


def api_version() -> dict[str, Any]:
    """The current version and, when one is known, the newer release."""
    with _lock:
        latest = _latest
    url = RELEASE_TAG_URL + latest if latest else None
    return {"current": __version__, "latest": latest, "url": url}


def _parse_version(tag: str) -> tuple[int, ...] | None:
    """A bare `major.minor.patch` tag as an integer tuple, or None when it is
    not exactly three dot-separated integers."""
    parts = tag.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_newer(latest: str, current: str = __version__) -> bool:
    """Whether `latest` is a strictly higher bare major.minor.patch than
    `current`. Either tag failing to parse means "no update"."""
    latest_parsed = _parse_version(latest)
    current_parsed = _parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    return latest_parsed > current_parsed


def _fetch_latest() -> str | None:
    """The `tag_name` of the repo's latest GitHub release, or None on any
    network, HTTP or decode failure. The one outbound call ccdash makes."""
    req = urllib.request.Request(
        LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
    # A self-check must never crash the thread or leak an error to a request:
    # every failure mode collapses to "no update".
    except Exception:
        return None
    tag = payload.get("tag_name")
    return tag if isinstance(tag, str) else None


def check_for_update() -> None:
    """Run one check and store the result: the newer release tag when GitHub
    reports one, cleared otherwise. Never raises."""
    global _latest
    tag = _fetch_latest()
    with _lock:
        _latest = tag if tag is not None and is_newer(tag) else None


def _check_loop() -> None:
    while True:
        check_for_update()
        time.sleep(CHECK_INTERVAL_SECONDS)


def update_check_enabled() -> bool:
    """Whether the daily release check runs. On unless
    `CCDASH_ENABLE_UPDATE_CHECK` is set to a falsy value -- the switch a fully
    offline instance flips."""
    value = os.environ.get("CCDASH_ENABLE_UPDATE_CHECK", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def start_update_check() -> None:
    """Spawn the daemon thread that checks for a newer release, unless the
    operator switched it off -- a no-op then, no thread and no outbound call."""
    if not update_check_enabled():
        return
    threading.Thread(
        target=_check_loop, name="ccdash-update-check", daemon=True
    ).start()
