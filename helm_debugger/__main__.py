"""Allow `python -m helm_debugger ./my-chart`."""

import sys

from .cli import main

sys.exit(main())
