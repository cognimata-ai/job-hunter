"""M5: the backup role's one way to un-ship a deletion manifest.

Revision ID: 0012_m5_unship_manifest
Revises: 0011_m5_purge_operation_index

Staged by the M5 final review's fix E2 (Ruling F-6; the final re-review's R3-I1), as static SQL, one
`op.execute` per statement:

  E2   unship_manifest(manifest_id bigint) RETURNS boolean: the prune's removal of a false receipt
       (`backup.prune`, spec §19.5 Retention) returns each manifest that receipt shipped to `completed`
       through it. In one UPDATE of the one manifest `manifest_id` that is `shipped`, it makes the
       registry's `shipped -> completed` assignments (`db.transitions`): the status, `completed_at` kept
       (`first_entry_kept`, so `privacy.manifest_unshipped` judges the deletion from its completion), and
       `shipped_at` and `shipment_id` cleared (`clear_on_exit`); the `assert_transition` trigger still
       checks the status change against `transition_matrix`. It returns true then, and false, changing
       nothing, for a manifest in any other state or for an id no manifest has. `SECURITY DEFINER`: it runs as
       its owner, the migrator who creates it, so the backup role holds no UPDATE on `deletion_manifests`,
       which would let a compromised backup container move an `executing` manifest to `completed` and so end
       an unfinished purge as done (R3-I1), or move a completion time. Its `search_path` is pinned to
       `public, pg_temp` (`pg_catalog` first, the temporary schema last; no role but the migrator may create
       in `public`, §19.3), which the trigger it fires reads `transition_matrix` through, and it names its
       table with the schema.
       Privileges: EXECUTE revoked from PUBLIC and granted to `jobhunter_backup` alone. `migrate`'s grants
       step applies both again on every run (`db.migrate.apply_explicit_grants`): a restore's `pg_restore
       --no-acl` leaves the function executable by PUBLIC. It also revokes the backup role's UPDATE on
       `deletion_manifests`, which the release before this one granted (final fix D1).

An expand migration, the one the upgrade-then-rollback drill crosses (`tests/stack/test_prod_drills.py`): it
adds a function and nothing else, so 0011's code runs on it, and its floor is 0011; it is not destructive. On
a rollback to that release, its `migrate` grants the backup role its UPDATE of the four columns again, and its
prune un-ships with it. The downgrade drops the function.
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0012_m5_unship_manifest"
down_revision: str | Sequence[str] | None = "0011_m5_purge_operation_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rollback compatibility floor (`db.compat`): 0011's code runs on this schema.
compat_floor: Final[str] = "0011_m5_purge_operation_index"
destructive: Final[bool] = False


def upgrade() -> None:
    op.execute("""\
CREATE FUNCTION unship_manifest(manifest_id bigint) RETURNS boolean
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE public.deletion_manifests AS t
     SET status = 'completed', completed_at = coalesce(t.completed_at, now()),
         shipped_at = NULL, shipment_id = NULL
   WHERE t.id = unship_manifest.manifest_id AND t.status = 'shipped';
  RETURN FOUND;
END $$;""")
    op.execute("REVOKE ALL ON FUNCTION unship_manifest(bigint) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION unship_manifest(bigint) TO jobhunter_backup;")


def downgrade() -> None:
    op.execute("DROP FUNCTION unship_manifest(bigint);")
