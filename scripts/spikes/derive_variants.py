"""Derive the hand-shaped fixture variants from scrubbed recordings (Task 6, Deviation 14, rulings (e), P7).

    uv run python scripts/spikes/derive_variants.py tests/contract/fixtures \\
        --jsearch-items-path data.jobs --jsearch-cursor-path data.cursor --arbeitnow-window 73

Recording a 429, a schema drift or an HTML error page from a paid provider costs quota and cannot be forced,
so those cases are derived here from the scrubbed files, each change listed in tests/contract/README.md. A
derivation keeps the wire structure (no key renamed or moved) and every identity relationship of the items it
keeps: a trim drops whole items and never edits one. The trims make a base structurally complete again
(Greenhouse `meta.total`, SmartRecruiters `totalFound`); the count-mismatch and pagination cases are variants
of their own, so a trim never erases those tests. Each trim applies only to an untrimmed base, so running the
script twice gives the same tree.

The JSearch variants find the item array and the cursor at the paths V9 recorded (`docs/spikes/V9.md`).
"""

import argparse
import copy
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

ARBEITNOW_TRIM: Final = 30
GREENHOUSE_TRIM: Final = 15
SMARTRECRUITERS_TRIM: Final = 10
LEVER_PAGE: Final = 100  # `limit=100`: a full Lever page continues with `skip`
HTML_ERROR_PAGE: Final = (
    '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="utf-8"><title>502 Bad Gateway</title></head>\n'
    "<body><h1>Bad Gateway</h1><p>The upstream server returned an invalid response.</p></body>\n</html>\n"
)
SERPAPI_NO_RESULTS: Final = "Google hasn't returned any results for this query."
ADZUNA_AUTH_FAILED: Final = {
    "__CLASS__": "Adzuna::API::Response::Exception",
    "display": "Authorisation failed",
    "doc": "https://api.adzuna.com/v1/doc",
    "exception": "AUTH_FAIL",
}
# The 404 of an unknown board, per ATS, where none was recorded (Greenhouse's is: `greenhouse_404`).
ATS_404: Final[dict[str, tuple[str, str, str]]] = {
    "lever": (".json", "application/json", json.dumps({"ok": False, "error": "Document not found"})),
    "ashby": (".json", "application/json", json.dumps({"success": False, "error": "Job board not found"})),
    "recruitee": (".json", "application/json", json.dumps({"error": "Not found"})),
    "smartrecruiters": (".json", "application/json", json.dumps({"message": "Company not found"})),
    "personio": (
        ".html",
        "text/html; charset=utf-8",
        "<!DOCTYPE html>\n<html><body><h1>Not Found</h1></body></html>\n",
    ),
}
_TOKEN: Final = re.compile(r"\[token:(\d+)\]")


def _path(spec: str) -> tuple[str, ...]:
    return tuple(part for part in spec.split(".") if part)


def _get(doc: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        doc = doc[key]
    return doc


def _set(doc: Any, path: tuple[str, ...], value: Any) -> None:
    _get(doc, path[:-1])[path[-1]] = value


class Tree:
    """The scrubbed fixture directory: read a case, write a derived one (body and headers)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.written: list[str] = []

    def body(self, case: str) -> Any:
        return json.loads((self.root / f"{case}.json").read_text("utf-8"))

    def headers(self, case: str) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads((self.root / f"{case}.headers.json").read_text("utf-8"))
        return doc

    def exists(self, case: str) -> bool:
        return (self.root / f"{case}.json").is_file() or (self.root / f"{case}.xml").is_file()

    def write(self, case: str, body: Any, headers: dict[str, Any], *, suffix: str = ".json") -> None:
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        (self.root / f"{case}{suffix}").write_text(text, encoding="utf-8")
        (self.root / f"{case}.headers.json").write_text(
            json.dumps(headers, indent=2) + "\n", encoding="utf-8"
        )
        self.written.append(f"{case}{suffix}")


def _with(
    headers: dict[str, Any], status: int | None = None, changes: dict[str, str] | None = None
) -> dict[str, Any]:
    """A copy of a `.headers.json` document with another status and changed header values."""
    out = copy.deepcopy(headers)
    if status is not None:
        out["status"] = status
    out["headers"].update(changes or {})
    return out


def jsearch(tree: Tree, items: tuple[str, ...], cursor: tuple[str, ...]) -> None:
    page, headers = tree.body("jsearch/search_v2_page1"), tree.headers("jsearch/search_v2_page1")
    empty = copy.deepcopy(page)
    _set(empty, items, [])
    _set(empty, cursor, None)  # the recorded end of results (`search_v2_today`) keeps the key, with null
    tree.write("jsearch/search_v2_empty", empty, headers)
    tree.write(
        "jsearch/search_v2_429",
        {"message": "Too many requests"},
        _with(headers, 429, {"x-ratelimit-requests-remaining": "0"}),
    )
    drift = copy.deepcopy(page)
    del drift["status"]
    tree.write("jsearch/search_v2_drift", drift, headers)
    html = _with(headers, 200, {"content-type": "text/html; charset=utf-8"})
    tree.write("jsearch/search_v2_html", HTML_ERROR_PAGE, html, suffix=".html")


def serpapi(tree: Tree) -> None:
    page, headers = tree.body("serpapi/google_jobs_hl_en"), tree.headers("serpapi/google_jobs_hl_en")
    empty = {
        k: copy.deepcopy(v)
        for k, v in page.items()
        if k not in ("jobs_results", "serpapi_pagination", "filters")
    }
    empty["error"] = SERPAPI_NO_RESULTS
    tree.write("serpapi/google_jobs_empty", empty, headers)
    tree.write(
        "serpapi/google_jobs_drift", {k: v for k, v in page.items() if k != "search_metadata"}, headers
    )


def _next_token(tree: Tree, source: str, *, exclude: str) -> int:
    """One more than the highest `[token:n]` of the source's files (`exclude` left out, so a rerun agrees)."""
    numbers = [
        int(m.group(1))
        for path in (tree.root / source).rglob("*.json")
        if not path.name.startswith(exclude)
        for m in _TOKEN.finditer(path.read_text("utf-8"))
    ]
    return max(numbers, default=0) + 1


def adzuna(tree: Tree) -> None:
    page, headers = tree.body("adzuna/search_p1_rpp100"), tree.headers("adzuna/search_p1_rpp100")
    empty = copy.deepcopy(page)
    empty["results"] = []
    tree.write("adzuna/search_empty", empty, headers)
    tree.write("adzuna/search_401", ADZUNA_AUTH_FAILED, _with(headers, 401))
    pair = copy.deepcopy(page)
    first = pair["results"][0]
    ids = {str(r["id"]) for r in pair["results"]}
    new_id = int(first["id"]) + 1
    while str(new_id) in ids:
        new_id += 1
    copy_of_first = copy.deepcopy(first)
    copy_of_first["id"] = str(new_id)
    # a fresh token: the scrubbed adref stands for a token that embeds the ad id, so the copy gets its own
    copy_of_first["adref"] = f"[token:{_next_token(tree, 'adzuna', exclude='adzuna_reingest_pair')}]"
    copy_of_first["redirect_url"] = first["redirect_url"].replace(f"/{first['id']}?", f"/{new_id}?")
    pair["results"][-1] = copy_of_first
    tree.write("adzuna/adzuna_reingest_pair", pair, headers)


def arbeitnow(tree: Tree, window: int) -> None:
    for case, start in (("arbeitnow/page1", window), ("arbeitnow/page2", 0)):
        if tree.exists(case):
            page = tree.body(case)
            if len(page["data"]) > ARBEITNOW_TRIM:
                page["data"] = page["data"][start : start + ARBEITNOW_TRIM]
                tree.write(case, page, tree.headers(case))
    page, headers = tree.body("arbeitnow/page1"), tree.headers("arbeitnow/page1")
    tree.write("arbeitnow/page1_remaining5", page, _with(headers, None, {"x-ratelimit-remaining": "5"}))
    text = json.dumps(page, ensure_ascii=False, indent=2)
    # cut just inside the first item: mid-array, and before any item value (a slug is key-shaped, and a body
    # that is not JSON is scanned as plain text, without the identifier allowlist)
    first = text.index("{", text.index('"data": [')) + 1
    tree.write("arbeitnow/page_truncated", text[:first], headers, suffix=".txt")
    end = copy.deepcopy(page)
    end["data"] = []
    end["links"]["next"] = None  # the recorded form of a missing link (`prev`, `last`): the key with null
    tree.write("arbeitnow/page_end", end, headers)


def ats(tree: Tree) -> None:
    gh = tree.body("ats/greenhouse_getyourguide")
    if len(gh["jobs"]) > GREENHOUSE_TRIM:
        gh["jobs"] = gh["jobs"][:GREENHOUSE_TRIM]
        gh["meta"]["total"] = len(gh["jobs"])
        tree.write("ats/greenhouse_getyourguide", gh, tree.headers("ats/greenhouse_getyourguide"))
    mismatch = copy.deepcopy(gh)
    mismatch["meta"]["total"] = len(gh["jobs"]) + 1
    tree.write("ats/greenhouse_count_mismatch", mismatch, tree.headers("ats/greenhouse_getyourguide"))

    sr_headers = tree.headers("ats/smartrecruiters_deliveryhero_list")
    sr = tree.body("ats/smartrecruiters_deliveryhero_list")
    if len(sr["content"]) > SMARTRECRUITERS_TRIM:
        sr["content"] = sr["content"][:SMARTRECRUITERS_TRIM]
        sr["totalFound"] = len(sr["content"])
        tree.write("ats/smartrecruiters_deliveryhero_list", sr, sr_headers)
    tree.write("ats/smartrecruiters_list_paged", {**copy.deepcopy(sr), "totalFound": 150}, sr_headers)
    tree.write(
        "ats/smartrecruiters_list_offset100",
        {**{k: v for k, v in sr.items() if k != "content"}, "offset": 100, "totalFound": 150, "content": []},
        sr_headers,
    )

    xml = (tree.root / "ats/personio_everphone.xml").read_text("utf-8")
    declaration, _, rest = xml.partition("?>")
    doctype = '\n<!DOCTYPE workzag-jobs [\n  <!ENTITY expanded "an entity the parser must refuse">\n]>'
    entity = declaration + "?>" + doctype + re.sub(r"<name>", "<name>&expanded; ", rest, count=1)
    tree.write("ats/personio_entity", entity, tree.headers("ats/personio_everphone"), suffix=".xml")

    if tree.exists("ats/lever_eu_leverdemo") and len(tree.body("ats/lever_eu_leverdemo")) == LEVER_PAGE:
        # the EU recording is a full page (100 postings): the page after it, empty, ends the traversal
        tree.write("ats/lever_eu_end", [], tree.headers("ats/lever_eu_leverdemo"))
    for board, (suffix, content_type, body) in ATS_404.items():
        tree.write(
            f"ats/{board}_404",
            body,
            {"status": 404, "headers": {"content-type": content_type}},
            suffix=suffix,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="derive_variants.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("fixtures", type=Path, help="the scrubbed fixture directory")
    parser.add_argument(
        "--jsearch-items-path", required=True, help="the item array of search-v2, e.g. data.jobs"
    )
    parser.add_argument(
        "--jsearch-cursor-path", required=True, help="the cursor of search-v2, e.g. data.cursor"
    )
    parser.add_argument(
        "--arbeitnow-window",
        type=int,
        default=0,
        help="the first item of the 30 kept from the recorded page 1",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    tree = Tree(args.fixtures)
    jsearch(tree, _path(args.jsearch_items_path), _path(args.jsearch_cursor_path))
    serpapi(tree)
    adzuna(tree)
    arbeitnow(tree, args.arbeitnow_window)
    ats(tree)
    print(f"derive_variants: {len(tree.written)} files written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
