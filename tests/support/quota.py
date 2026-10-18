"""Quota test helpers (Task 7); Task 8 adds the ledger helpers here."""

from typing import Final

from jobhunter.run_settings import QuotaSettings

# The seeded `quota` settings (`settings_v1.json`, Task 5): JSearch 200, SerpApi 250, reserve 10 %, SerpApi
# hourly cap 45, Adzuna 20 / 225 / 900 / 2,250, `paid_max_requests_per_run` auto.
SETTINGS: Final = QuotaSettings(200, 250, 10, 45, 20, 225, 900, 2250, "auto")
