"""The state-machine registry and `transition()`, the one way a stateful row changes state (spec §7.16).

`TRANSITIONS` holds, per table, the allowed edges and the column effects of an edge. `transition()` checks a
call against it and runs a single CAS `UPDATE`: the new state, the reason, the changed-at column and the
cleared ownership columns are all part of that statement, so a row never leaves `running` still owned.

The statement is built with SQLAlchemy Core over a table object made from the registry. Every identifier in
it (table, state, reason, changed-at, owner, cleared and settable columns) comes from the registry, never from
a caller string: a caller's table, states, owner keys and `set_` keys are looked up in the registry and
refused when they are not there. Caller values are always bound parameters.

The database holds the same edges in `transition_matrix` (written by `seed.sync_transition_matrix` from this
registry), where the `assert_transition` trigger checks them as defence in depth.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.errors import TransitionConflict


@dataclass(frozen=True)
class TransitionSpec:
    """What the registry knows about one table's state machine.

    `changed_at` maps a to-state to the column that records entering it. `owner_cols` are the columns an
    ownership predicate may test. `clear_on_exit` maps a state to the columns set to NULL when a row leaves it
    for another state. `settable` are the other columns a caller may assign in the same update (`set_`); the
    columns the helper manages itself are not among them.
    """

    table: str
    state_col: str
    reason_col: str | None
    changed_at: Mapping[str, str]
    owner_cols: frozenset[str]
    clear_on_exit: Mapping[str, tuple[str, ...]]
    allowed: frozenset[tuple[str, str]]
    settable: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        states = {state for edge in self.allowed for state in edge}
        unknown = (set(self.changed_at) | set(self.clear_on_exit)) - states
        if unknown:
            raise ValueError(f"{self.table}: states {sorted(unknown)} are in no allowed edge")
        managed = {self.state_col, *self.changed_at.values()}
        if self.reason_col is not None:
            managed.add(self.reason_col)
        if clash := self.settable & managed:
            raise ValueError(f"{self.table}: {sorted(clash)} are managed by transition() and not settable")

    @property
    def columns(self) -> frozenset[str]:
        """Every column the helper may name in a statement on this table."""
        named = {"id", self.state_col, *self.changed_at.values(), *self.owner_cols, *self.settable}
        if self.reason_col is not None:
            named.add(self.reason_col)
        for cleared in self.clear_on_exit.values():
            named.update(cleared)
        return frozenset(named)


_RUN_TERMINAL: Final = ("succeeded", "partial", "failed_invariant", "failed", "cancelled")

TRANSITIONS: Final[Mapping[str, TransitionSpec]] = MappingProxyType(
    {
        # §7.16.4. A run keeps its owner columns on exit: its ownership predicate is `attempt_no`.
        "runs": TransitionSpec(
            table="runs",
            state_col="status",
            reason_col="status_reason",
            changed_at=MappingProxyType(
                {"running": "started_at", **dict.fromkeys(_RUN_TERMINAL, "finished_at")}
            ),
            owner_cols=frozenset({"attempt_no"}),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("queued", "running"),
                    ("running", "running"),
                    ("running", "succeeded"),
                    ("running", "partial"),
                    ("running", "failed_invariant"),
                    ("running", "failed"),
                    ("queued", "failed"),
                    ("queued", "cancelled"),
                    ("running", "cancelled"),
                }
            ),
            settable=frozenset(
                {"attempt_no", "current_thread_id", "resume_count", "heartbeat_at", "report", "llm_tuple_id"}
            ),
        ),
        # §7.16.8. The claim, its epoch and its heartbeat go together on every exit from `running`.
        "operations": TransitionSpec(
            table="operations",
            state_col="status",
            reason_col="last_error_code",
            changed_at=MappingProxyType(
                {
                    "running": "started_at",
                    "succeeded": "finished_at",
                    "failed": "finished_at",
                    "cancelled": "finished_at",
                }
            ),
            owner_cols=frozenset({"claim_id"}),
            clear_on_exit=MappingProxyType({"running": ("claim_id", "worker_epoch", "heartbeat_at")}),
            allowed=frozenset(
                {
                    ("pending", "running"),
                    ("running", "succeeded"),
                    ("running", "failed"),
                    ("running", "pending"),
                    ("pending", "cancelled"),
                    ("running", "cancelled"),
                }
            ),
            settable=frozenset(
                {"claim_id", "worker_epoch", "heartbeat_at", "attempts", "not_before", "progress"}
            ),
        ),
    }
)


@cache
def _table(name: str) -> sa.TableClause:
    """A lightweight table object over the registry columns of `name` (only registered names get here)."""
    spec = TRANSITIONS[name]
    columns = (
        sa.column(column, sa.BigInteger) if column == "id" else sa.column(column)
        for column in sorted(spec.columns)
    )
    return sa.table(spec.table, *columns)


def _bound(value: object) -> object:
    """`value` for the SET clause: SQL expressions as they are, JSON containers as JSONB, the rest bound."""
    if isinstance(value, sa.ClauseElement):
        return value
    if isinstance(value, Mapping):
        return sa.literal(dict(value), JSONB)
    if isinstance(value, list):
        return sa.literal(value, JSONB)
    return value


def _validate(
    spec: TransitionSpec,
    from_states: Sequence[str],
    to_state: str,
    reason: str | None,
    owner: Mapping[str, object],
    set_: Mapping[str, object],
) -> frozenset[str]:
    """Check a call against the registry; return the columns the exit from `from_states` clears."""
    if not from_states:
        raise ValueError(f"{spec.table}: from_states is empty")
    for from_state in from_states:
        # A from-state that appears in no edge is not a state of the table: it is refused like any other edge.
        if (from_state, to_state) not in spec.allowed:
            raise ValueError(f"disallowed transition {spec.table}: {from_state} -> {to_state}")
    if reason is not None and spec.reason_col is None:
        raise ValueError(f"{spec.table} has no reason column")
    if foreign := set(owner) - spec.owner_cols:
        raise ValueError(f"{spec.table}: {sorted(foreign)} are not ownership columns")
    cleared = frozenset(
        column
        for from_state in from_states
        if from_state != to_state
        for column in spec.clear_on_exit.get(from_state, ())
    )
    if overlap := set(set_) & cleared:
        raise ValueError(f"{spec.table}: {sorted(overlap)} are cleared by this transition and cannot be set")
    if unsettable := set(set_) - spec.settable:
        raise ValueError(f"{spec.table}: {sorted(unsettable)} cannot be set by a transition")
    return cleared


async def transition(
    conn: AsyncConnection,
    table: str,
    row_id: int,
    from_states: Sequence[str],
    to_state: str,
    *,
    reason: str | None = None,
    owner: Mapping[str, object] | None = None,
    set_: Mapping[str, object] | None = None,
) -> None:
    """Move row `row_id` of `table` from one of `from_states` to `to_state`, or raise.

    One `UPDATE ... WHERE id = :id AND <state> = ANY(:from) [AND <owner col> = :owner ...] RETURNING id`:

    - The new state, and the reason when `reason` is given.
    - The changed-at column of `to_state`, unless the row is already in `to_state` (a self-edge keeps the
      time of the first entry).
    - `NULL` for the ownership columns that `clear_on_exit` lists for every state of `from_states` other
      than `to_state`.
    - The `set_` columns. A value that is a SQL expression is rendered as it is, a mapping or a list is bound
      as JSONB, anything else as an ordinary parameter.

    A self-edge (for example `running -> running`) is exclusive only when `owner` is given: without an owner
    predicate, two callers that both name `running` as the from-state both match, and the last one wins. A
    caller that needs exclusivity (a takeover that must beat its rival) passes the owner.

    Raises `ValueError`, before touching the database, for a table or an edge the registry does not know, an
    owner key that is not an ownership column, and a `set_` key that is not settable or that the transition
    clears. Raises `TransitionConflict` when no row matched: it is absent, in another state, or owned by
    another attempt or claim. The caller decides what that means.
    """
    spec = TRANSITIONS.get(table)
    if spec is None:
        raise ValueError(f"unknown table {table!r}: it has no state machine")
    owner = owner or {}
    set_ = set_ or {}
    cleared = _validate(spec, from_states, to_state, reason, owner, set_)

    target = _table(spec.table)
    state = target.c[spec.state_col]
    assignments: dict[str, object] = {spec.state_col: to_state}
    if reason is not None and spec.reason_col is not None:
        assignments[spec.reason_col] = reason
    changed_at = spec.changed_at.get(to_state)
    if changed_at is not None:
        assignments[changed_at] = (
            sa.case((state == to_state, target.c[changed_at]), else_=sa.func.now())
            if to_state in from_states
            else sa.func.now()
        )
    assignments.update(dict.fromkeys(sorted(cleared), sa.null()))
    assignments.update({column: _bound(value) for column, value in set_.items()})

    statement = (
        sa.update(target)
        .where(
            target.c.id == row_id,
            state == sa.any_(sa.literal(list(from_states), ARRAY(sa.Text))),
            # The owner value keeps no type of its own, so the database reads it as the column's type.
            *(
                target.c[column] == sa.bindparam(None, value, type_=sa.types.NullType())
                for column, value in owner.items()
            ),
        )
        .values(assignments)
        .returning(target.c.id)
    )
    if (await conn.execute(statement)).first() is None:
        raise TransitionConflict(table, row_id, to_state)
