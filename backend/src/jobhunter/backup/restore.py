"""The restore of a backup set's dump (spec §19.5 "Restore order" step 4; plan M5 Tasks 25 and 26, R23).

`restore_list(listing)` is the filtered list of `pg_restore -L`: the entries of a dump's `pg_restore --list`
output that a restore runs, every entry but those of what the bootstrap superuser created in the database
before the restore (`deploy/postgres/init/10-init.sh`, and the template a scratch database is created from):
the extensions (`EXTENSION`), their comments (`COMMENT - EXTENSION`) and the `public` schema (`SCHEMA -
public`, which PostgreSQL 17 dumps only when its definition differs from the default). The migrator and the
restore role own none of them, so `pg_restore --no-owner --no-acl --exit-on-error` would stop at the first.
Every other entry is kept, in its order and with its text, so the restore runs exactly the dump's statements.

A line of the listing is a comment (`;` first), blank, or an entry: `<dump id>; <catalog oid> <object oid>
<description> <schema> <name> <owner>`, where an object outside a schema has `-` for its schema. A line of
any other shape is no listing `pg_restore` wrote: it raises `ValueError`, and no list is built on it. It is a
pure function of the text, which Task 25's test harness (`tests/support/restore.py`) and Task 26's `restore
run` step 4 both run.
"""

import re
from typing import Final

_ENTRY: Final = re.compile(r"\d+; \d+ \d+ (?P<what>\S.*)")
# The entries the bootstrap superuser owns, by their description, schema and name (module docstring).
_SUPERUSER_OWNED: Final = re.compile(r"(?:EXTENSION - \S+|COMMENT - EXTENSION \S+|SCHEMA - public)(?: .*)?")


def restore_list(listing: str) -> list[str]:
    """The entries of `listing` (a dump's `pg_restore --list` output) to restore, in their order (module
    docstring); `ValueError` for a line that is neither a comment nor an entry."""
    entries: list[str] = []
    for line in listing.splitlines():
        if not line.strip() or line.startswith(";"):
            continue
        entry = _ENTRY.fullmatch(line)
        if entry is None:
            raise ValueError("a line of the listing is no entry of pg_restore --list")
        if not _SUPERUSER_OWNED.fullmatch(entry["what"]):
            entries.append(line)
    return entries
