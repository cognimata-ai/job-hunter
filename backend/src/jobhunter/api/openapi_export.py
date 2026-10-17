"""Write the api's OpenAPI 3.1 document (spec §14.5): `python -m jobhunter.api.openapi_export <path>`.

It builds the app without a lifespan: no configuration, database or secret is needed.
"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from jobhunter.api.app import create_app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobhunter.api.openapi_export")
    parser.add_argument("path", type=Path, help="where to write the OpenAPI document (JSON)")
    args = parser.parse_args(argv)
    document = create_app().api.openapi()
    args.path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
