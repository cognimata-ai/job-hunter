"""The upload rejection (spec §12.1; M2 Task 9), in a module of its own (M2 Task 11).

`profile.upload` raises it and the api maps it to its problem; the worker names it too, as an error whose text
is a code (`graphs.profile.graph.sanitized` passes it through). `profile.upload` imports FastAPI, which the
worker never imports, so the class lives here and `profile.upload` re-exports it.
"""

from typing import Literal

RejectReason = Literal[
    "too_large",
    "unknown_type",
    "extension_mismatch",
    "docx_structure",
    "docx_macro",
    "docx_expansion",
    # a pasted text
    "too_short",
    "too_long",
    "not_utf8",
]


class UploadRejected(Exception):
    """A file or a pasted text failed a check of §12.1; `reason` is the problem's `errors[0].code`."""

    def __init__(self, reason: RejectReason) -> None:
        super().__init__(reason)
        self.reason: RejectReason = reason
