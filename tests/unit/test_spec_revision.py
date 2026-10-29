"""Revision 3.5 of the design spec (M3 Task 25) agrees with the code it describes.

The spec is prose, so only the values that the code also holds are pinned here:
- the status line names revision 3.5 and keeps the text of revisions 3.3 and 3.4;
- §11.5's JSON block is the generator wire schema of prompt-schema version 2, candidate C of spike V4b
  (`scripts/spikes/generator_v2_candidates.candidate_c`), compared as canonical JSON;
- §6.8's dry-run divides by the Arbeitnow page yield of `quota/dryrun.py` (V8's 17.5 h, Task 24);
- §11.3's anchor of a mandatory language with an `unspecified` level is the sentence of rubric v2 (R13).
"""

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

from jobhunter.llm.fake import canonical_schema_json
from jobhunter.quota.dryrun import ARBEITNOW_HOURS_PER_PAGE
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs/superpowers/specs/2026-10-16-job-hunter-design.md"


def _spec_lines() -> list[str]:
    return SPEC.read_text(encoding="utf-8").splitlines()


def _section(heading: str) -> list[str]:
    """The lines of the section that starts with `heading`, up to the next heading of its level or above."""
    lines = _spec_lines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading)]
    assert len(starts) == 1, f"{heading!r} heads {len(starts)} sections"
    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        marks = len(line) - len(line.lstrip("#"))
        if 0 < marks <= level and line[marks : marks + 1] == " ":
            break
        body.append(line)
    return body


def _candidates() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generator_v2_candidates", REPO / "scripts/spikes/generator_v2_candidates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_status_line_names_revision_3_5_and_keeps_the_earlier_revisions() -> None:
    status = [line for line in _spec_lines() if line.startswith("| Status |")]
    assert len(status) == 1
    assert status[0].startswith("| Status | **Approved** — revision 3.5. Revision 3.3 was approved")
    assert "Revision 3.4 (2026-10-21, the user's decision after spike V19)" in status[0]
    assert "Revision 3.5 (" in status[0]


def test_the_generator_schema_of_11_5_is_candidate_c() -> None:
    body = _section("### 11.5 ")
    opens = [i for i, line in enumerate(body) if line == "```json"]
    assert len(opens) == 1, "§11.5 holds exactly one JSON block"
    close = body.index("```", opens[0] + 1)
    block = json.loads("\n".join(body[opens[0] + 1 : close]))
    assert canonical_schema_json(block) == canonical_schema_json(_candidates().candidate_c())


def test_the_arbeitnow_page_yield_of_6_8_is_the_dry_run_constant() -> None:
    divisors = [
        float(match.group(1))
        for line in _section("### 6.8 ")
        if (match := re.search(r"expected_pages = proposed\.interval_h / ([0-9]+(?:\.[0-9]+)?) \+ 1", line))
    ]
    assert divisors == [ARBEITNOW_HOURS_PER_PAGE]


def test_the_language_anchor_of_11_3_is_rubric_v2s_sentence() -> None:
    languages = load_rubric(RUBRIC_V2).anchors["languages"]
    assert isinstance(languages, dict)
    sentence = languages["unspecified_level"]
    assert isinstance(sentence, str)
    head = "A mandatory language with an `unspecified` level"
    assert sentence.startswith(head)
    paragraphs = [line for line in _section("### 11.3 ") if head in line]
    assert len(paragraphs) == 1
    assert paragraphs[0][paragraphs[0].index(head) :] == sentence
