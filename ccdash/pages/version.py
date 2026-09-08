"""The running version, reported to the sidebar footer.

The update check is not built yet: `latest` and `url` are always null, the
placeholders a later release-comparison will fill.
"""

from typing import Any

from .. import __version__


def api_version() -> dict[str, Any]:
    """The current version, with the update-check fields still unfilled."""
    return {"current": __version__, "latest": None, "url": None}
