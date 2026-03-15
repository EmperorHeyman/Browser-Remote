"""
Seamless browser remote - Configuration

Reads from %APPDATA%/ProjectorRemote/config.json (exe-safe, no source rewriting).
Falls back to sensible defaults if no config file exists.
"""
import json
import os

_APPDATA_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "ProjectorRemote"
)
CONFIG_PATH = os.path.join(_APPDATA_DIR, "config.json")

_DEFAULTS = {
    "brave_path": r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "cdp_port": 9222,
    "user_data_dir": os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "BraveSoftware", "Brave-Browser", "User Data",
    ),
    "host": "0.0.0.0",
    "port": 5000,
    "projector_monitor": 0,
    "monitors": {},
    "default_url": "https://www.youtube.com/tv",
    "no_browser": False,
    "sites": {
        "youtube": "https://www.youtube.com/tv",
        "netflix": "https://www.netflix.com/browse",
        "oneplay": "https://www.oneplay.cz",
    },
    "cursor_hide_delay": 2.0,
    "cursor_sensitivity": 1.0,
    "scroll_speed": 1.0,
    "startup_urls": [],
    "favorites": [],
    "language": "en",
}


def load() -> dict:
    """Load config from disk, merged with defaults for any missing keys."""
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {**_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(overrides: dict | None = None):
    """Save config to %APPDATA%. Pass overrides to merge with existing."""
    os.makedirs(_APPDATA_DIR, exist_ok=True)
    cfg = load()
    if overrides:
        cfg.update(overrides)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Module-level variables (backward-compatible with `import config`)
# ---------------------------------------------------------------------------
def _reload():
    """Reload from disk and update module-level variables."""
    global BRAVE_PATH, CDP_PORT, USER_DATA_DIR, HOST, PORT
    global PROJECTOR_MONITOR, MONITORS, DEFAULT_URL, SITES
    global CURSOR_HIDE_DELAY, CURSOR_SENSITIVITY, SCROLL_SPEED
    global STARTUP_URLS, FAVORITES, LANGUAGE
    cfg = load()
    BRAVE_PATH = cfg["brave_path"]
    CDP_PORT = cfg["cdp_port"]
    USER_DATA_DIR = cfg["user_data_dir"]
    HOST = cfg["host"]
    PORT = cfg["port"]
    PROJECTOR_MONITOR = cfg["projector_monitor"]
    MONITORS = cfg["monitors"]
    DEFAULT_URL = cfg["default_url"]
    SITES = cfg["sites"]
    CURSOR_HIDE_DELAY = cfg["cursor_hide_delay"]
    CURSOR_SENSITIVITY = cfg["cursor_sensitivity"]
    SCROLL_SPEED = cfg["scroll_speed"]
    STARTUP_URLS = cfg["startup_urls"]
    FAVORITES = cfg["favorites"]
    LANGUAGE = cfg["language"]


_reload()
