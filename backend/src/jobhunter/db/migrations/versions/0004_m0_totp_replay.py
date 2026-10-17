"""M0 TOTP replay protection: the time step of the last TOTP code a login accepted (ruling T16-a).

Revision ID: 0004_m0_totp_replay
Revises: 0003_m0_transition_matrix_grants

A TOTP code stays valid for its 30 s step and, with the ±1 step window of §16.2, for up to 90 s. RFC 6238
§5.2 says a verifier must not accept a code a second time once it validated. `users.totp_last_step` holds
the step of the code the last successful login used; a login accepts only a code of a later step
(`api.routes.auth.decide_login`). NULL means no TOTP login since TOTP was last turned on;
`users.disable_totp` clears it. The spec has no such column; the plan records it as a deviation.

The column is nullable and has no default, so adding it rewrites no rows. Migrations are expand/contract:
there is no downgrade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_m0_totp_replay"
down_revision: str | Sequence[str] | None = "0003_m0_transition_matrix_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""\
-- RFC 6238 §5.2: a TOTP code is accepted once; a login needs a step later than the last one used
ALTER TABLE users ADD COLUMN totp_last_step bigint;""")


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
