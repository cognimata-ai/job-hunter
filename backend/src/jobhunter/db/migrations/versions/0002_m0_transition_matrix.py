"""M0 transition matrix: the defence-in-depth trigger of the state machines (spec §7.16).

Revision ID: 0002_m0_transition_matrix
Revises: 0001_m0_baseline

`transition_matrix` holds the allowed (from, to) edges per table. The code registry
(`jobhunter.db.transitions.TRANSITIONS`) is its source: `migrate` replaces the rows on every run
(`seed.sync_transition_matrix`), so this migration creates the table empty. `assert_transition()` is a
generic trigger function; it reads the state column named by its first trigger argument and refuses an
UPDATE that changes it to a pair not in the matrix (SQLSTATE 23514). The two triggers cover `runs` and
`operations`; a later milestone adds one trigger per further state machine.

Migrations are expand/contract: there is no downgrade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_m0_transition_matrix"
down_revision: str | Sequence[str] | None = "0001_m0_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""\
CREATE TABLE transition_matrix (table_name text NOT NULL, from_state text NOT NULL, to_state text NOT NULL,
                                PRIMARY KEY (table_name, from_state, to_state));""")
    op.execute("""\
CREATE FUNCTION assert_transition() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o text := to_jsonb(OLD) ->> TG_ARGV[0]; n text := to_jsonb(NEW) ->> TG_ARGV[0];
BEGIN
  IF o IS DISTINCT FROM n AND NOT EXISTS (SELECT 1 FROM transition_matrix
       WHERE table_name = TG_TABLE_NAME AND from_state = o AND to_state = n) THEN
    RAISE EXCEPTION 'disallowed transition %: % -> %', TG_TABLE_NAME, o, n USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;""")
    op.execute(
        "CREATE TRIGGER runs_assert_transition BEFORE UPDATE OF status ON runs "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )
    op.execute(
        "CREATE TRIGGER operations_assert_transition BEFORE UPDATE OF status ON operations "
        "FOR EACH ROW EXECUTE FUNCTION assert_transition('status');"
    )


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
