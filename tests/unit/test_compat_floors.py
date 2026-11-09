"""The rollback compatibility floor of every revision of the migration chain (`db.compat`, M5 Task 1)."""

import importlib
from types import ModuleType

import pytest
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.compat import CompatInfo, chain_compat, compat_of

M4_SCHEMA = "0009_m4_ui"
M5_SCHEMA = "0010_m5_operations"
# The revisions before M5, base first: none declares a floor, so each is its own.
BEFORE_M5 = (
    "0001_m0_baseline",
    "0002_m0_transition_matrix",
    "0003_m0_transition_matrix_grants",
    "0004_m0_totp_replay",
    "0005_m1_collection",
    "0006_m2_llm_profile_settings",
    "0007_m2_draft_text_edits",
    "0008_m3_scoring",
    M4_SCHEMA,
)


def migration(revision: str) -> ModuleType:
    return importlib.import_module(f"jobhunter.db.migrations.versions.{revision}")


def test_every_revision_has_a_floor_on_its_own_chain() -> None:
    chain = chain_compat()
    revisions = [info.revision for info in chain]
    # every revision of the package, base first, each after the revision it revises
    assert revisions[0] == BEFORE_M5[0] and revisions[-1] == code_head()
    assert [migration(revision).down_revision for revision in revisions] == [None, *revisions[:-1]]
    # a floor is the revision itself or one before it on the chain
    for position, info in enumerate(chain):
        assert info.compat_floor in revisions[: position + 1], info
    # the modules without a declaration: their own revision, and not destructive
    assert chain[: len(BEFORE_M5)] == tuple(CompatInfo(r, r, False) for r in BEFORE_M5)
    for revision in BEFORE_M5:
        assert not hasattr(migration(revision), "compat_floor") and not hasattr(
            migration(revision), "destructive"
        )


def test_0010_floor_is_0009() -> None:
    module = migration(M5_SCHEMA)
    assert (module.revision, module.down_revision) == (M5_SCHEMA, M4_SCHEMA)
    assert (module.compat_floor, module.destructive) == (M4_SCHEMA, False)
    assert {info.revision: info for info in chain_compat()}[M5_SCHEMA] == CompatInfo(
        M5_SCHEMA, M4_SCHEMA, False
    )


def fake(revision: str, down_revision: str | None, **declared: object) -> ModuleType:
    """A migration module of `revision` after `down_revision`, with the `declared` attributes."""
    module = ModuleType(f"fake_{revision}")
    vars(module).update(revision=revision, down_revision=down_revision, **declared)
    return module


def test_a_fake_chain_reads_the_declarations_and_their_defaults() -> None:
    modules = [
        fake("a", None),
        fake("b", "a", compat_floor="a", destructive=False),
        fake("c", "b", compat_floor="c", destructive=True),
        fake("d", "c", compat_floor="a", destructive=False),  # a floor further back than the last revision
    ]
    assert compat_of(modules) == (
        CompatInfo("a", "a", False),
        CompatInfo("b", "a", False),
        CompatInfo("c", "c", True),
        CompatInfo("d", "a", False),
    )


@pytest.mark.parametrize(
    ("modules", "message"),
    [
        # a floor after its revision: the newer revision is no ancestor of it
        ([fake("a", None), fake("b", "a", compat_floor="c"), fake("c", "b")], "b: compat_floor 'c'"),
        # a floor that is no revision of the chain at all
        (
            [fake("a", None), fake("b", "a", compat_floor="0009_elsewhere")],
            "b: compat_floor '0009_elsewhere'",
        ),
        # a floor on a branch the revision does not descend from
        (
            [fake("a", None), fake("x", "a"), fake("b", "a", compat_floor="x")],
            "b: compat_floor 'x'",
        ),
        # declarations of the wrong type
        ([fake("a", None), fake("b", "a", compat_floor=9)], "b: compat_floor must be a str"),
        ([fake("a", None), fake("b", "a", destructive="no")], "b: destructive must be a bool"),
    ],
)
def test_a_floor_outside_the_chain_raises(modules: list[ModuleType], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compat_of(modules)
