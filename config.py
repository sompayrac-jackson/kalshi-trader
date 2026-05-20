"""
Central config — reads all secrets from environment variables.

Local dev: create a .env file (see .env.example), it is loaded automatically.
Production: set env vars in the systemd service file (see DEPLOY.md).
"""

import os
from pathlib import Path


def _load_dotenv():
    """Minimal .env loader — no external dependencies required."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # Strip inline comments and surrounding quotes
        val = val.split("#")[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


_load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(
            f"\n\nRequired environment variable '{key}' is not set.\n"
            f"  -> Copy .env.example to .env and fill in your values, or\n"
            f"  -> Set it in your systemd service Environment= lines.\n"
        )
    return val


# ── Secrets (required) ────────────────────────────────────────────────────────

KALSHI_API_KEY  = _require("KALSHI_API_KEY")

# ── Optional config ───────────────────────────────────────────────────────────

ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH", "private_key.pem")

# Dashboard HTTP Basic Auth (strongly recommended for production)
# Leave DASHBOARD_PASS empty to disable auth (local dev only)
DASHBOARD_USER   = os.getenv("DASHBOARD_USER", "kalshi")
DASHBOARD_PASS   = os.getenv("DASHBOARD_PASS", "")

# Pushover push notifications (live trades only — dry-run is never notified)
# Get these from https://pushover.net after installing the app
PUSHOVER_USER_KEY  = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")
