"""Pure validation of finite numbers and bounded text fields."""
import math


def number(value) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite number in API response')
    return result


def label(value, limit=64) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('Invalid text field in API response')
    return value
