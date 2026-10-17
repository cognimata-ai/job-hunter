"""M0 transition matrix grants: the application role may read the matrix, not edit it.

Revision ID: 0003_m0_transition_matrix_grants
Revises: 0002_m0_transition_matrix

The default privileges of `deploy/postgres/init` give `jobhunter_app` DML on every table the migrator creates,
which includes `transition_matrix`. The matrix is what the `assert_transition` trigger trusts, and it is
written only by `migrate` (as the migrator, `seed.sync_transition_matrix`), so the application role loses
INSERT, UPDATE, DELETE and TRUNCATE on it. SELECT stays: the trigger function runs as the invoking role, so
an application `UPDATE runs SET status ...` needs to read the matrix.

This is a separate revision because migrations are append-only once committed (expand/contract): there is
no downgrade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_m0_transition_matrix_grants"
down_revision: str | Sequence[str] | None = "0002_m0_transition_matrix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON transition_matrix FROM jobhunter_app;")


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
