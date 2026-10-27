"""Allow ``python -m dspace_sentinel``."""

from __future__ import annotations

import sys

try:
    from dspace_sentinel.cli import main
except ImportError as exc:  # pragma: no cover - exercised through a subprocess in test_cli
    # A half-installed virtualenv must not exit 1, which the CLI uses for "warnings".
    sys.stderr.write(
        f"error: cannot start DSpace Sentinel: {exc}\n"
        "       The virtualenv looks incomplete; run ./sentinel.sh install again.\n"
    )
    sys.exit(4)

if __name__ == "__main__":
    sys.exit(main())
