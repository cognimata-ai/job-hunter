"""The ATS board adapters (spec §5.3.5): the only sources whose absence can close a posting (§8.11).

`adapter_for` builds the adapter of a watchlist board's source instance. The board is the instance's
`instance_key` (`instances.instance_for_company`): the slug, or `<region>:<site>` for Lever; it always exists,
also once the watchlist company is gone. The company name of postings that name none is the watchlist
company's display name, else the slug.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

import httpx

from jobhunter.sources.ats.ashby import AshbyAdapter
from jobhunter.sources.ats.common import AtsAdapter
from jobhunter.sources.ats.greenhouse import GreenhouseAdapter
from jobhunter.sources.ats.lever import LeverAdapter
from jobhunter.sources.ats.personio import PersonioAdapter
from jobhunter.sources.ats.recruitee import RecruiteeAdapter
from jobhunter.sources.ats.smartrecruiters import SmartRecruitersAdapter
from jobhunter.sources.instances import InstanceRow

__all__ = [
    "AshbyAdapter",
    "AtsAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "PersonioAdapter",
    "RecruiteeAdapter",
    "SmartRecruitersAdapter",
    "adapter_for",
]

# The adapters whose board is the slug alone (Lever's is `<region>:<site>`).
_BY_SLUG: Final[Mapping[str, type[AtsAdapter]]] = MappingProxyType(
    {
        "greenhouse": GreenhouseAdapter,
        "ashby": AshbyAdapter,
        "personio": PersonioAdapter,
        "recruitee": RecruiteeAdapter,
        "smartrecruiters": SmartRecruitersAdapter,
    }
)


def adapter_for(
    instance: InstanceRow, client: httpx.AsyncClient, *, clock: Callable[[], datetime]
) -> AtsAdapter:
    """The adapter of an ATS source instance, on `client` with `clock`.

    Raises `ValueError` for an instance that is not an ATS board, a Lever key without a valid region, and a
    slug outside `watchlist_patterns.SLUG`.
    """
    company = instance.display_name
    if instance.source_id == "lever":
        region, separator, site = instance.instance_key.partition(":")
        if not separator or region not in ("global", "eu"):
            raise ValueError(
                f"a Lever instance key is <region>:<site>, region global or eu: {instance.instance_key!r}"
            )
        return LeverAdapter(
            client,
            instance_id=instance.id,
            slug=site,
            region="global" if region == "global" else "eu",
            clock=clock,
            company=company,
        )
    adapter = _BY_SLUG.get(instance.source_id)
    if adapter is None:
        raise ValueError(f"source {instance.source_id!r} is not an ATS")
    return adapter(client, instance_id=instance.id, slug=instance.instance_key, clock=clock, company=company)
