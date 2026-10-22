"""M2 the draft's stored text edit set: `profile_drafts.text_edits` (M2 Task 14, ruling R14-1).

Revision ID: 0007_m2_draft_text_edits
Revises: 0006_m2_llm_profile_settings

`PUT /api/profile/drafts/{id}/text` takes the draft's complete edit set and rebuilds `cv_files.stripped_text`
from the raw text (plan Deviation 29). A client that reloads the review page knows only what the server
returns, so the set is stored with the draft and `GET /api/profile/drafts/{id}` returns it; without it a
reloaded page would send a set without the earlier removals, and a span the user removed would come back.

The column holds the set as `{"restored": [[start, end], ...], "removed": [[start, end], ...]}`: offsets into
the raw text, never a character of it (`profile.contacts.Edits`). A new draft starts with the empty set (the
default); the edit from `ready` back to `text_review` keeps it. The CHECK keeps the two lists: `IS NOT
DISTINCT FROM`, so that a missing member (`->` gives NULL) is refused too and not passed as NULL (the lesson
of plan Deviation 30). §20.1's purges need not reset it: a purged draft keeps offsets only, and no route
applies them without the raw text they point into.

The spec's §7.4 DDL has no such column: the deviation is recorded with the task (plan Deviation 37). The
column has a constant default, so adding it rewrites no rows; the CHECK reads the table once. Migrations are
expand/contract: there is no downgrade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_m2_draft_text_edits"
down_revision: str | Sequence[str] | None = "0006_m2_llm_profile_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""\
-- the complete text edit set of the review: offsets into the raw text only (R14-1; plan Deviation 37)
ALTER TABLE profile_drafts ADD COLUMN text_edits jsonb NOT NULL DEFAULT '{"restored": [], "removed": []}'
  CONSTRAINT profile_drafts_text_edits_check
  CHECK (jsonb_typeof(text_edits -> 'restored') IS NOT DISTINCT FROM 'array'
         AND jsonb_typeof(text_edits -> 'removed') IS NOT DISTINCT FROM 'array');""")


def downgrade() -> None:
    raise NotImplementedError("expand/contract: no downgrades")
