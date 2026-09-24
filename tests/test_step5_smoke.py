"""Tests for scratch/step5_smoke.py — the step 5 smoke run's scratch module (#168)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scratch" / "step5_smoke.py"

spec = importlib.util.spec_from_file_location("step5_smoke", SCRIPT)
step5_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(step5_smoke)


def test_double_positive():
    assert step5_smoke.double(21) == 42


def test_double_zero_and_negative():
    assert step5_smoke.double(0) == 0
    assert step5_smoke.double(-3) == -6
