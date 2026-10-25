"""`scripts/check_llm_boundary.py`, the AST half of the §10.1 boundary (plan Task 5, F24).

It fails on `bind_tools`, `tool_choice`, `temperature`, `top_p` and `top_k` as a name, an attribute or a
keyword argument (never as a string constant), on `with_structured_output` without `method="json_schema"`,
and on an `AIMessage` as the last element of a message list or tuple literal.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
BANNED = ("bind_tools", "tool_choice", "temperature", "top_p", "top_k")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_llm_boundary", REPO / "scripts/check_llm_boundary.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


boundary = _load()


def rules(source: str) -> list[str]:
    return [v.rule for v in boundary.check_source(source, "snippet.py")]


@pytest.mark.parametrize("name", BANNED)
def test_a_banned_name_fails(name: str) -> None:
    assert rules(f"value = {name}\n") == ["banned_name"]


@pytest.mark.parametrize("name", BANNED)
def test_a_banned_attribute_fails(name: str) -> None:
    assert rules(f"llm.{name}\n") == ["banned_name"]
    assert rules(f"llm.{name}([tool])\n") == ["banned_name"]


@pytest.mark.parametrize("name", BANNED)
def test_a_banned_keyword_argument_fails(name: str) -> None:
    assert rules(f"ChatX(model='m', {name}=1)\n") == ["banned_name"]


@pytest.mark.parametrize("name", BANNED)
def test_a_string_literal_naming_a_banned_key_passes(name: str) -> None:
    assert rules(f'assert "{name}" not in body\n') == []
    assert rules(f'body = {{"{name}": None}}\nbody.pop("{name}")\n') == []


def test_the_violation_names_its_line_and_construct() -> None:
    (violation,) = boundary.check_source("x = 1\ny = llm.temperature\n", "snippet.py")
    assert (violation.path, violation.line, violation.rule, violation.name) == (
        "snippet.py",
        2,
        "banned_name",
        "temperature",
    )


def test_with_structured_output_needs_method_json_schema() -> None:
    assert rules("llm.with_structured_output(Schema)\n") == ["structured_output_method"]
    assert rules("llm.with_structured_output(Schema, method='function_calling')\n") == [
        "structured_output_method"
    ]
    assert rules("llm.with_structured_output(Schema, **options)\n") == ["structured_output_method"]
    assert rules("method = llm.with_structured_output\n") == ["structured_output_method"]
    assert rules("with_structured_output(llm, Schema)\n") == ["structured_output_method"]


def test_the_gateways_own_structured_output_call_passes() -> None:
    source = 'llm.with_structured_output(schema, method="json_schema", include_raw=True)\n'
    assert rules(source) == []
    gateway = (REPO / "backend/src/jobhunter/llm/gateway.py").read_text("utf-8")
    assert boundary.check_source(gateway, "gateway.py") == []


def test_an_ai_message_last_in_a_message_list_fails() -> None:
    assert rules("messages = [HumanMessage('q'), AIMessage('prefill')]\n") == ["assistant_prefill"]
    assert rules("messages = (SystemMessage('s'), lc.AIMessage(content='{'))\n") == ["assistant_prefill"]
    assert rules("messages = [AIMessage('alone')]\n") == ["assistant_prefill"]


def test_an_ai_message_before_the_last_user_turn_passes() -> None:
    assert rules("messages = [AIMessage('earlier'), HumanMessage('q')]\n") == []
    assert rules("isinstance(raw, AIMessage)\n") == []
    assert rules("isinstance(raw, (HumanMessage, AIMessage))\n") == []  # a type tuple, not messages


def test_the_repository_is_clean() -> None:
    assert boundary.check_paths([REPO / p for p in boundary.DEFAULT_ROOTS]) == []
    assert boundary.main([]) == 0


def test_main_reports_a_violation_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("llm.bind_tools([tool])\n", "utf-8")
    assert boundary.main([str(bad)]) == 1
    out = capsys.readouterr().out
    assert f"{bad}:1: banned_name bind_tools" in out
