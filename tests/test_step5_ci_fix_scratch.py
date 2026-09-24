"""Scratch tests for the step 5 CI-fix and review-fix runs; must not be merged."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scratch.step5_review import safe_div  # noqa: E402


def test_safe_div_divides():
    assert safe_div(6.0, 3.0) == 2.0


def test_safe_div_by_zero_returns_zero():
    assert safe_div(1.0, 0.0) == 0.0
