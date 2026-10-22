"""`/api/geo` (spec §8.12, §14.2; plan Task 22, Deviation 3): the gazetteer search behind the scope-location
autocomplete.

`GET /api/geo/places?q=<name or postal code>&limit=10` → `[{gazetteer_id, label, postal_codes, lat, lon,
population}]`, the places `Gazetteer.search` gives, served from the bundled data with no network and no
database. `q` is folded by the search itself (`münch` and `Munich` both list München first; a postal code
lists its place) and may be empty, which lists the most populous places. It has at most 100 characters and no
NUL character (422 `validation_failed` otherwise), and `limit` is 1 to 25 (default 10). `label` is the
place's name. The gazetteer is loaded on the first request, off the event loop, and kept on the app state
(`deps.get_gazetteer`).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from jobhunter.api.deps import get_gazetteer, require_session
from jobhunter.api.models import CleanStr
from jobhunter.api.openapi import SESSION_RESPONSES
from jobhunter.geo.gazetteer import Gazetteer

router = APIRouter(prefix="/api/geo", dependencies=[Depends(require_session)], responses=SESSION_RESPONSES)

MAX_QUERY_CHARS: Final = 100
DEFAULT_LIMIT: Final = 10
MAX_LIMIT: Final = 25


class PlaceOut(BaseModel):
    gazetteer_id: str
    label: str
    postal_codes: list[str]
    lat: float
    lon: float
    population: int


@router.get(
    "/places",
    response_model=list[PlaceOut],
    responses={422: {"description": "A `q` over 100 characters or with a NUL, or a `limit` outside 1 to 25"}},
)
async def search_places(
    gazetteer: Annotated[Gazetteer, Depends(get_gazetteer)],
    q: Annotated[
        CleanStr,
        Query(
            max_length=MAX_QUERY_CHARS, description="A place name or a postal code; empty lists the largest"
        ),
    ] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[PlaceOut]:
    """Search places by name, other spelling or postal code (200), the most populous first."""
    return [
        PlaceOut(
            gazetteer_id=place.gazetteer_id,
            label=place.name,
            postal_codes=list(place.postal_codes),
            lat=place.lat,
            lon=place.lon,
            population=place.population,
        )
        for place in gazetteer.search(q, limit)
    ]
