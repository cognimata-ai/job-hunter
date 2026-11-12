"""M5: the purge operations by their payload's manifest id.

Revision ID: 0011_m5_purge_operation_index
Revises: 0010_m5_operations

Staged by docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md (Task 27; Ruling T27-2), as
static SQL, one `op.execute` per statement:

  T27  operations_purge_manifest: the `purge` operations by `payload -> 'manifest_id'`, the expression their
       readers compare (`payload -> 'manifest_id' = to_jsonb(<manifest id>)`): the purge-fences reconciler's
       newest operation of each executing manifest and its check for a live or failed one
       (`privacy/reconcilers.py`, `_EXECUTING`), retention's check that an old purge operation belongs to an
       executing manifest (`privacy/retention.py`, the `operations` rule), and delete-everything's lookups of
       its own manifest's operations (`privacy/kinds/delete_everything.py`). Without it each of them reads
       every operation of the kind. The table is small (operations are pruned by retention), so the build
       takes no `CONCURRENTLY`.

An expand migration, the one the upgrade-then-rollback drill crosses (`tests/stack/test_prod_drills.py`): it
adds an index and nothing else, so M5's first schema's code (0010's) runs on it, and its floor is 0010; it is
not destructive. A destructive migration declares `destructive = True` and itself as its floor
(`db.compat`). The downgrade drops the index.
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0011_m5_purge_operation_index"
down_revision: str | Sequence[str] | None = "0010_m5_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rollback compatibility floor (`db.compat`): 0010's code runs on this schema.
compat_floor: Final[str] = "0010_m5_operations"
destructive: Final[bool] = False


def upgrade() -> None:
    op.execute(
        "CREATE INDEX operations_purge_manifest ON operations ((payload -> 'manifest_id')) "
        "WHERE kind = 'purge';"
    )


def downgrade() -> None:
    op.execute("DROP INDEX operations_purge_manifest;")
