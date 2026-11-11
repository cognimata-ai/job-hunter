"""`backup.restore.restore_list` (spec §19.5 "Restore order" step 4; plan M5 Task 25): the entries of a dump's
`pg_restore --list` that a restore runs. The listing lines here are written in the format `pg_restore` prints
(`<dump id>; <catalog oid> <object oid> <description> <schema> <name> <owner>`); a real dump's listing is
checked in `tests/integration/test_typed_replay.py`."""

import pytest
from jobhunter.backup.restore import restore_list

HEADER = (
    ";\n; Archive created at 2026-11-11 15:37:09 UTC\n;     dbname: jobhunter\n;\n"
    "; Selected TOC Entries:\n;\n"
)


def test_the_superusers_entries_are_left_out_and_the_rest_kept_in_order() -> None:
    listing = (
        HEADER
        + "3; 3079 16717 EXTENSION - pg_trgm \n"
        + "5420; 0 0 COMMENT - EXTENSION pg_trgm \n"
        + "2; 3079 16389 EXTENSION - vector \n"
        + "5422; 0 0 COMMENT - EXTENSION vector \n"
        + "7; 2615 2200 SCHEMA - public pg_database_owner\n"
        + "8; 2615 17500 SCHEMA - publicity jobhunter_migrator\n"
        + "301; 1259 17001 TABLE public jobs jobhunter_migrator\n"
        + "302; 0 17001 COMMENT public TABLE jobs jobhunter_migrator\n"
        + "\n"
        + "5001; 0 17001 TABLE DATA public jobs jobhunter_migrator\n"
        + "5100; 0 0 ACL - SCHEMA public pg_database_owner\n"
    )
    assert restore_list(listing) == [
        "8; 2615 17500 SCHEMA - publicity jobhunter_migrator",
        "301; 1259 17001 TABLE public jobs jobhunter_migrator",
        "302; 0 17001 COMMENT public TABLE jobs jobhunter_migrator",
        "5001; 0 17001 TABLE DATA public jobs jobhunter_migrator",
        "5100; 0 0 ACL - SCHEMA public pg_database_owner",
    ]


def test_a_listing_of_comments_alone_has_no_entry() -> None:
    assert restore_list(HEADER) == []
    assert restore_list("") == []


@pytest.mark.parametrize(
    "line",
    [
        "not an entry",
        "301 1259 17001 TABLE public jobs jobhunter_migrator",  # no `;` after the dump id
        "301; 1259 TABLE public jobs jobhunter_migrator",  # an oid missing
        "301; 1259 17001 ",  # no description
        " ; a comment that does not start the line",
    ],
)
def test_a_line_of_another_shape_is_refused(line: str) -> None:
    with pytest.raises(ValueError, match="no entry of pg_restore --list"):
        restore_list(HEADER + line + "\n")
