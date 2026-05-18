"""Strategy implementations.

Each strategy is a small, testable unit with two responsibilities:
1. Given a list of TokenSnapshots, decide which (if any) to enter.
2. Given an open position + current snapshot, decide whether to exit.

Strategies do NOT touch the database or the wallet. The runner/backtester
threads positions through them.
"""

from .base import Position, Strategy, StrategyDecision
from .momentum import MomentumSniperS1
from .migration import MigrationSniperS2
from .scalp import MajorsScalpS3

__all__ = [
    "Position",
    "Strategy",
    "StrategyDecision",
    "MomentumSniperS1",
    "MigrationSniperS2",
    "MajorsScalpS3",
]
