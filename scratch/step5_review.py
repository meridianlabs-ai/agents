"""Scratch fixture for the step 5 review-fix run; must not be merged."""


def safe_div(a: float, b: float) -> float:
    """Return a / b, or 0.0 when b is 0."""
    return 0.0 if b == 0 else a / b
