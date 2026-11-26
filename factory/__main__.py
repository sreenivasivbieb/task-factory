"""``python -m factory``: the same as the ``factory`` command."""

import sys

from .cli import main

sys.exit(main())
