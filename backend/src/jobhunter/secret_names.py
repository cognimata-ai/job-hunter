"""The names of the secret files of spec §19.2.

This module imports nothing, so the log redaction (`observability/redact.py`) and `config.py` can both
use it without an import cycle.
"""

from typing import Final

# Every secret file name that spec §19.2 lists, and nothing else.
SECRET_NAMES: Final[frozenset[str]] = frozenset(
    {
        "session_secret",
        "data_key",
        "data_key_previous",
        "db_app",
        "db_migrator",
        "db_backup",
        "db_restore",
        "pg_bootstrap",
        "anthropic_key",
        "rapidapi_key",
        "serpapi_key",
        "adzuna_app_id",
        "adzuna_app_key",
        "langsmith_key",
        "ntfy_token",
        "healthchecks_ping_url",
        "healthchecks_api_key",
        "restic_password",
        "s3_access_key_id",
        "s3_secret_access_key",
    }
)
