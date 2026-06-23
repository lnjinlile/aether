from .base import BaseStrategy, Signal, SignalType

# ── PERF-096: Centralized MR strategy name patterns ──
# Single source of truth for identifying mean-reversion strategies.
# Used by engine.py (regime gate + signal multiplier) and mercury_run.py (position sizing).
# When adding a new MR strategy, update ONLY this tuple.
MR_PATTERN_NAMES = (
    "_MR_", "MR_", "RSI_",
    "DonchianMR", "KeltnerMR", "BandMR",
    "BBandRSI", "StochRSI", "BBand", "MeanRev"
)

__all__ = ["BaseStrategy", "Signal", "SignalType", "MR_PATTERN_NAMES"]
