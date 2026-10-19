"""The versioning keys of the deterministic algorithms (spec §11.11)."""

from typing import Final

# Names the hard-filter, knockout and aggregation code. It is stored on every `hard_filter_results` row (and,
# from M3, on every `det_results` row); a deployment that changes it makes the completeness reconcilers
# recompute the current rows with no LLM call.
DET_ALGORITHM_VERSION: Final = "1"
