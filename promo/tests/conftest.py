"""Pytest conftest — Sprint 15 hoists the repo-root path prepend that
previously lived at the top of ``test_promo_module.py``.

No fixtures, no hooks. Path resolution only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
