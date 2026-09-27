"""Make the sample repo importable when pytest runs from its root."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
