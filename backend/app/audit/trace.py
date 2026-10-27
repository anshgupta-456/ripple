"""Process-wide monotonically increasing counter used to order trace entries that share a simulated timestamp."""
import itertools

_c = itertools.count(1)


def next_seq() -> int:
    return next(_c)
