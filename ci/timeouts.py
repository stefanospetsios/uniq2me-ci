"""Zero minute budgets disable a deadline; infinities stay internal, never in JSON."""
import math


def budget_seconds(minutes):
    return math.inf if minutes == 0 else minutes * 60


def optional_seconds(seconds):
    return None if seconds is None or math.isinf(seconds) else seconds


def allowance(seconds):
    return 'unlimited' if seconds is None or math.isinf(seconds) else f'{max(0, seconds):.1f}s'
