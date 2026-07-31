"""Plugin configuration loaded from a ``.env`` file.

The plugin ships a ``.env`` file in its root folder (next to
``metadata.txt``) holding the LOC server origin:

    LOC_BASE_URL=https://dashboard.useloc.com

Everything is derived from that one origin: the API talks to
``{LOC_BASE_URL}/api/v1`` and web links (the location page opened after
a push, the dashboard button) open ``{LOC_BASE_URL}/...``.

Resolution order: OS environment variable ``LOC_BASE_URL`` first (so a
developer can override without editing files), then the ``.env`` file,
then the built-in default.  No third-party dotenv dependency — QGIS
Python environments cannot be relied on to have one.
"""

import os
import logging

DEFAULT_BASE_URL = "https://dashboard.useloc.com"

_log = logging.getLogger("LOC.config")

# Plugin root = parent of this services/ package.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_PLUGIN_DIR, ".env")

_cache = None  # parsed .env contents, read once per QGIS session


def _read_env_file(path):
    """Parse a minimal KEY=VALUE .env file into a dict.

    Supports blank lines, ``#`` comments, and optional single/double
    quotes around the value.  Anything malformed is skipped.
    """
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key:
                    values[key] = val
    except OSError as exc:
        _log.debug("No .env file read (%s): %s", path, exc)
    return values


def _env_values():
    global _cache
    if _cache is None:
        _cache = _read_env_file(_ENV_PATH)
    return _cache


def get_base_url():
    """The LOC server origin, e.g. ``https://dashboard.useloc.com``."""
    url = (
        os.environ.get("LOC_BASE_URL", "").strip()
        or _env_values().get("LOC_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    return url.rstrip("/")


def get_api_base_url():
    """The API root derived from the configured origin."""
    return f"{get_base_url()}/api/v1"
