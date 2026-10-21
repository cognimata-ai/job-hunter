"""M2: rubric v1, prompt-schema version 1, the active pointers to them, and the restored NOT NULL.

Revision ID: 0007_m2_rubric_prompt_seed
Revises: 0006_m2_llm_profile_settings

Source: docs/superpowers/specs/2026-10-16-job-hunter-design.md §7.3, §11.2, §11.3; plan
docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md (Task 6), closing M0 Deviation 1.

Two halves, in one transaction:
1. **Rows.** Rubric `v1` (`scoring.rubric.load_rubric("v1")`: the §11.2 weights and §11.3 anchors) and
   prompt-schema version 1 (`llm.prompts.render.prompt_schema_v1()`: the two M2 templates and the four
   transformed schemas) are inserted from those pure functions, each `ON CONFLICT DO NOTHING` on its hash.
2. **Pointers.** Where an `active_config` row exists (an install upgraded from M1) and its rubric or
   prompt-schema pointer is NULL, the pointer is set to the row of half 1. Then both columns are
   `SET NOT NULL`, as the spec's DDL has them. On a fresh install `active_config` is still empty here (the
   seed step of `migrate` runs after every migration), so nothing is updated and `seed_defaults` inserts the
   row with both pointers.

§19.3 step 2 keeps rows out of migrations so that no identity value is allocated before migrate's identity
advancement. Half 1 is the one exception the restored NOT NULL needs: an upgraded `active_config` row must
point at rows that exist before the constraint can hold. It is harmless to that guarantee: no deletion
manifest ever names a `rubric_versions` or `prompt_schema_versions` id (they hold no personal content, and
delete-everything keeps them, §20.4), and on a restored database step 1 has already advanced both
identities.

The pure functions are imported inside `upgrade()`: Alembic imports every revision module to build its
revision map (`alembic_runner.code_head` does so in the API process), and only this revision's run needs the
renderer and the rubric loader.

Migrations are expand/contract: there is no downgrade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_m2_rubric_prompt_seed"
down_revision: str | Sequence[str] | None = "0006_m2_llm_profile_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from jobhunter.db.sql import q
    from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1
    from jobhunter.scoring.rubric import RUBRIC_V1, load_rubric

    bind = op.get_bind()
    rubric = load_rubric(RUBRIC_V1)
    templates, schemas = prompt_schema_v1()
    prompt_hash = prompt_schema_hash(templates, schemas)

    bind.execute(
        q(
            "INSERT INTO rubric_versions (version, content_hash, weights, anchors) "
            "VALUES (:version, :hash, :weights, :anchors) ON CONFLICT (content_hash) DO NOTHING",
            jsonb=["weights", "anchors"],
        ),
        {
            "version": RUBRIC_V1,
            "hash": rubric.content_hash,
            "weights": dict(rubric.weights),
            "anchors": dict(rubric.anchors),
        },
    )
    bind.execute(
        q(
            "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas) "
            "VALUES (:hash, :templates, :schemas) ON CONFLICT (prompt_schema_hash) DO NOTHING",
            jsonb=["templates", "schemas"],
        ),
        {"hash": prompt_hash, "templates": templates, "schemas": schemas},
    )

    bind.execute(
        q(
            "UPDATE active_config SET "
            "rubric_version_id = coalesce(rubric_version_id, "
            "(SELECT id FROM rubric_versions WHERE content_hash = :rubric_hash)), "
            "prompt_schema_version_id = coalesce(prompt_schema_version_id, "
            "(SELECT id FROM prompt_schema_versions WHERE prompt_schema_hash = :prompt_hash)), "
            "updated_at = now() "
            "WHERE rubric_version_id IS NULL OR prompt_schema_version_id IS NULL"
        ),
        {"rubric_hash": rubric.content_hash, "prompt_hash": prompt_hash},
    )
    op.execute("ALTER TABLE active_config ALTER COLUMN rubric_version_id SET NOT NULL;")
    op.execute("ALTER TABLE active_config ALTER COLUMN prompt_schema_version_id SET NOT NULL;")


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
