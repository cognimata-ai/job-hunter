"""scripts/spikes/derive_variants.py (Task 6, Deviation 14, rulings (e) and P7): hand-shaped variants derived
from scrubbed recordings, on a small synthetic tree in the recorded wire shapes."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/spikes/derive_variants.py"


@pytest.fixture(scope="module")
def derive() -> ModuleType:
    spec = importlib.util.spec_from_file_location("derive_variants", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def put(root: Path, rel: str, doc: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = doc if isinstance(doc, str) else json.dumps(doc)
    path.write_text(text, encoding="utf-8")
    base = rel.rsplit(".", 1)[0]
    (root / f"{base}.headers.json").write_text(
        json.dumps({"status": status, "headers": {"content-type": "application/json", **(headers or {})}})
    )


def read(root: Path, rel: str) -> Any:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def tree(root: Path) -> None:
    put(
        root,
        "ats/lever_eu_leverdemo.json",
        [{"id": f"{i:08d}-0000-0000-0000-000000000000"} for i in range(100)],
    )
    jobs = [{"job_uid": f"{i:022d}==", "job_title": "Dev"} for i in range(10)]
    put(
        root,
        "jsearch/search_v2_page1.json",
        {"status": "OK", "request_id": "[token:1]", "data": {"jobs": jobs, "cursor": "[token:2]"}},
        headers={"x-ratelimit-requests-remaining": "198", "x-ratelimit-requests-limit": "200"},
    )
    put(
        root,
        "serpapi/google_jobs_hl_en.json",
        {
            "search_metadata": {"id": "[token:1]"},
            "search_parameters": {},
            "filters": [],
            "jobs_results": [{"job_id": "x"}],
            "serpapi_pagination": {"next_page_token": "[token:2]"},
        },
    )
    results = [
        {
            "id": str(5933514000 + i),
            "adref": f"[token:{i + 1}]",
            "redirect_url": f"https://www.adzuna.de/details/{5933514000 + i}?utm_medium=api"
            "&utm_source=scrubbed",
        }
        for i in range(50)
    ]
    put(
        root,
        "adzuna/search_p1_rpp100.json",
        {"__CLASS__": "R", "count": 516, "mean": 1.0, "results": results},
    )
    data = [{"slug": f"job-{i}", "location": "Berlin", "created_at": 1000 - i} for i in range(300)]
    put(
        root,
        "arbeitnow/page1.json",
        {
            "data": data,
            "links": {
                "first": "f",
                "last": None,
                "prev": None,
                "next": "https://www.arbeitnow.com/api/job-board-api?page=2",
            },
            "meta": {"per_page": 300},
        },
        headers={"x-ratelimit-limit": "50", "x-ratelimit-remaining": "49"},
    )
    put(
        root,
        "ats/greenhouse_getyourguide.json",
        {"jobs": [{"id": i} for i in range(70)], "meta": {"total": 70}},
    )
    put(
        root,
        "ats/smartrecruiters_deliveryhero_list.json",
        {
            "offset": 0,
            "limit": 100,
            "totalFound": 938,
            "content": [{"id": str(744000 + i)} for i in range(100)],
        },
    )
    put(
        root,
        "ats/personio_everphone.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<workzag-jobs><position>'
        "<id>1</id><name>Dev</name></position></workzag-jobs>",
        headers={"content-type": "text/xml"},
    )


def test_every_variant_and_trim(derive: ModuleType, tmp_path: Path) -> None:
    tree(tmp_path)
    assert (
        derive.main(
            [
                str(tmp_path),
                "--jsearch-items-path",
                "data.jobs",
                "--jsearch-cursor-path",
                "data.cursor",
                "--arbeitnow-window",
                "73",
            ]
        )
        == 0
    )
    page1 = read(tmp_path, "jsearch/search_v2_page1.json")
    empty = read(tmp_path, "jsearch/search_v2_empty.json")
    assert empty["data"] == {"jobs": [], "cursor": None} and empty["status"] == "OK"  # the recorded end form
    assert read(tmp_path, "jsearch/search_v2_429.headers.json")["status"] == 429
    assert (
        read(tmp_path, "jsearch/search_v2_429.headers.json")["headers"]["x-ratelimit-requests-remaining"]
        == "0"
    )
    drift = read(tmp_path, "jsearch/search_v2_drift.json")
    assert "status" not in drift and drift["data"] == page1["data"]
    assert (
        (tmp_path / "jsearch/search_v2_html.html").read_text().lstrip().lower().startswith("<!doctype html")
    )
    assert read(tmp_path, "jsearch/search_v2_html.headers.json")["headers"]["content-type"].startswith(
        "text/html"
    )

    serp = read(tmp_path, "serpapi/google_jobs_empty.json")
    assert "jobs_results" not in serp and "serpapi_pagination" not in serp and serp["error"]
    assert "search_metadata" not in read(tmp_path, "serpapi/google_jobs_drift.json")

    assert read(tmp_path, "adzuna/search_empty.json")["results"] == []
    assert read(tmp_path, "adzuna/search_401.headers.json")["status"] == 401
    pair = read(tmp_path, "adzuna/adzuna_reingest_pair.json")["results"]
    base = read(tmp_path, "adzuna/search_p1_rpp100.json")["results"]
    assert len(pair) == 50 and pair[:49] == base[:49] and pair[-1]["id"] not in {r["id"] for r in base}
    assert {k: v for k, v in pair[-1].items() if k not in ("id", "adref", "redirect_url")} == {
        k: v for k, v in pair[0].items() if k not in ("id", "adref", "redirect_url")
    }
    assert f"/details/{pair[-1]['id']}?" in pair[-1]["redirect_url"] and pair[-1]["adref"] != pair[0]["adref"]

    an = read(tmp_path, "arbeitnow/page1.json")
    assert [d["slug"] for d in an["data"]] == [
        f"job-{i}" for i in range(73, 103)
    ]  # one contiguous window, whole items
    assert (
        read(tmp_path, "arbeitnow/page1_remaining5.headers.json")["headers"]["x-ratelimit-remaining"] == "5"
    )
    assert read(tmp_path, "arbeitnow/page1_remaining5.json") == an
    truncated = (tmp_path / "arbeitnow/page_truncated.txt").read_text()
    assert truncated.startswith('{\n  "data": [') and truncated.rstrip().endswith("{")  # cut inside the array
    with pytest.raises(ValueError):
        json.loads(truncated)
    end = read(tmp_path, "arbeitnow/page_end.json")
    assert end["data"] == [] and end["links"]["next"] is None

    gh = read(tmp_path, "ats/greenhouse_getyourguide.json")
    assert (
        len(gh["jobs"]) == 15
        and gh["meta"]["total"] == 15
        and [j["id"] for j in gh["jobs"]] == list(range(15))
    )
    assert read(tmp_path, "ats/greenhouse_count_mismatch.json")["meta"]["total"] == 16
    sr = read(tmp_path, "ats/smartrecruiters_deliveryhero_list.json")
    assert len(sr["content"]) == 10 and sr["totalFound"] == 10
    assert read(tmp_path, "ats/smartrecruiters_list_paged.json")["totalFound"] == 150
    assert read(tmp_path, "ats/smartrecruiters_list_offset100.json") | {} == {
        "offset": 100,
        "limit": 100,
        "totalFound": 150,
        "content": [],
    }
    assert "<!ENTITY" in (tmp_path / "ats/personio_entity.xml").read_text()
    assert (
        read(tmp_path, "ats/lever_eu_end.json") == []
        and read(tmp_path, "ats/lever_eu_end.headers.json")["status"] == 200
    )
    for ats in ("lever", "ashby", "recruitee", "smartrecruiters", "personio"):
        assert read(tmp_path, f"ats/{ats}_404.headers.json")["status"] == 404


def test_deriving_twice_gives_the_same_tree(derive: ModuleType, tmp_path: Path) -> None:
    tree(tmp_path)
    args = [
        str(tmp_path),
        "--jsearch-items-path",
        "data.jobs",
        "--jsearch-cursor-path",
        "data.cursor",
        "--arbeitnow-window",
        "73",
    ]
    assert derive.main(args) == 0
    first = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert derive.main(args) == 0  # the trims are idempotent: a trimmed base is never trimmed again
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == first
