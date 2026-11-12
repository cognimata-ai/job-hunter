"""A test-only revision: a newer release's head, which the code under test does not know (plan M5 Task 27).

Revision ID: 9999_test_ahead
Revises: this package's head, whatever it is (`code_head()`)

An expand migration: one new empty table. Its floor is the package's head, so the code under test, an older
release to it, runs on the schema it leaves; it is not destructive. Only `tests.support.fake_revision`'s
Alembic configuration loads it.
"""

from collections.abc import Sequence
from typing import Final

from alembic import op
from jobhunter.db.alembic_runner import code_head

revision: str = "9999_test_ahead"
down_revision: str | Sequence[str] | None = code_head()
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

compat_floor: Final[str] = code_head()
destructive: Final[bool] = False


def upgrade() -> None:
    op.execute("CREATE TABLE test_ahead (id bigint PRIMARY KEY, status text NOT NULL);")


def downgrade() -> None:
    op.execute("DROP TABLE test_ahead;")
