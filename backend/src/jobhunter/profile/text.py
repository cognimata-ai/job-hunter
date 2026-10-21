"""The rule every stored CV text keeps (M2 Task 11; Task 9 carry): it holds no NUL character.

PostgreSQL's `text` type refuses U+0000, and a pasted text (JSON carries `\\u0000`) or a docparse result (a
PDF's text map can read a glyph as U+0000) may hold one. `drop_nul` removes it before a text is stored: the
paste (`upload.check_paste`) before it is sealed, and docparse's text (the `parse` node) before
`cv_files.raw_text_enc` is written. Every later text derives from that stored raw text (`stripped_text`,
Task 14's rebuild from the decrypted raw text), so its offsets stay consistent.
"""

from typing import Final

NUL: Final = "\x00"


def drop_nul(text: str) -> str:
    """`text` without its NUL characters."""
    return text.replace(NUL, "")
