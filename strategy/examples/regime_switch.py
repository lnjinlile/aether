"""
Regime-Switching Strategy — HMM or clustering-based market state detection.

Detects market regimes (trending, ranging, high-vol, low-vol) and dynamically
switches between sub-strategies optimized for each regime.

Sub-strategies per regime:
- TRENDING  -> TrendFollow (EMA slope based)
- RANGING   -> Mean Reversion (RSI-based grid entries)
- HIGH_VOL  -> Reduce exposure or stay flat
- LOW_VOL   -> DynamicGrid (harvest small oscillations)

State detection:
- Primary: Gaussian HMM (Hidden Markov Model) on returns + volatility
- Fallback: KMeans clustering on volatility/trend metrics
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..base import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


class Regime(Enum):
    """Market regime classification."""
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    UNKNOWN = "UNKNOWN"


@dataclass
class RegimeStats:
    """Per-regime statistics for monitoring."""
    regime: Regime
    count: int = 0
    avg_return: float = 0.0
    avg_volatility: float = 0.0


# ---------------------------------------------------------------------------
# Sub-strategy signal generators (inlined, no BaseStrategy dependency)
# ---------------------------------------------------------------------------

def _trend_follow_signal(
    df: pd.DataFrame,
    ema_period: int = 50,
    sl_pct: float = 0.015,
    tp_pct: float = 0.05,
    cooldown_bars: int = 5,
    leverage: int = 3,
    strategy_name: str = "RegimeSwitch",
) -> Signal:
    """Generate trend-following sub-signal."""
    close = df["close"].values
    if len(close) < ema_period + 20:
        return Signal(SignalType.HOLD, "", reason="Not enough data for TF")

    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    slope = ema[-1] - ema[-6]  # 5-bar slope
    price = float(close[-1])
    uptrend = slope > 0

    if uptrend:
        return Signal(
            SignalType.LONG, "",
            price=price, quantity=0.001,
            stop_loss=price * (1 - sl_pct),
            take_profit=price * (1 + tp_pct),
            reason=f"TrendFollow sub (EMA{ema_period} uptrend)",
            confidence=0.65, leverage=leverage,
            strategy_name=strategy_name,
            timestamp=df.index[-1],
        )
    else:
        return Signal(
            SignalType.SHORT, "",
            price=price, quantity=0.001,
            stop_loss=price * (1 + sl_pct),
            take_profit=price * (1 - tp_pct),
            reason=f"TrendFollow sub (EMA{ema_period} downtrend)",
            confidence=0.65, leverage=leverage,
            strategy_name=strategy_name,
            timestamp=df.index[-1],
        )


def _mean_reversion_signal(
    df: pd.DataFrame,
    rsi_period: int = 14,
    oversold: int = 30,
    overbought: int = 70,
    exit_rsi: int = 50,
    sl_pct: float = 0.02,
    tp_pct: float = 0.04,
    leverage: int = 2,
    strategy_name: str = "RegimeSwitch",
) -> Signal:
    """Generate mean-reversion sub-signal (RSI-based)."""
    close = df["close"]
    price = float(close.iloc[-1])

    if len(close) < rsi_period + 5:
        return Signal(SignalType.HOLD, "", reason="Not enough data for MR")

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(span=rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_val = float(rsi.iloc[-1])

    if rsi_val < oversold:
        return Signal(
            SignalType.LONG, "",
            price=price, quantity=0.001,
            stop_loss=price * (1 - sl_pct),
            take_profit=price * (1 + tp_pct),
            reason=f"MeanReversion sub (RSI={rsi_val:.0f} oversold)",
            confidence=0.70, leverage=leverage,
            strategy_name=strategy_name,
            timestamp=df.index[-1],
        )
    elif rsi_val > overbought:
        return Signal(
            SignalType.SHORT, "",
            price=price, quantity=0.001,
            stop_loss=price * (1 + sl_pct),
            take_profit=price * (1 - tp_pct),
            reason=f"MeanReversion sub (RSI={rsi_val:.0f} overbought)",
            confidence=0.70, leverage=leverage,
            strategy_name=strategy_name,
            timestamp=df.index[-1],
        )
    else:
        return Signal(
            SignalType.HOLD, "",
            reason=f"MeanReversion sub (RSI={rsi_val:.0f} neutral)",
            strategy_name=strategy_name,
            timestamp=df.index[-1],
        )


# ---------------------------------------------------------------------------
# Regime Detector
# ---------------------------------------------------------------------------

class RegimeDetector:
    """Detect market regime using HMM + clustering fallback.

    Uses Gaussian HMM on 2D data: [returns, volatility].
    Falls back to heuristic clustering if HMM fails or hmmlearn not available.
    """

    def __init__(
        self,
        n_regimes: int = 4,
        hmm_lookback: int = 100,
        volatility_window: int = 20,
        trend_window: int = 50,
    ):
        self.n_regimes = n_regimes
        self.hmm_lookback = hmm_lookback
        self.volatility_window = volatility_window
        self.trend_window = trend_window

        self._hmm = None
        self._regime_map: Dict[int, Regime] = {}
        self._hmm_fitted = False

    def _init_hmm(self):
        """Lazy-init the HMM model."""
        if self._hmm is not None:
            return
        try:
            from hmmlearn.hmm import GaussianHMM
            self._hmm = GaussianHMM(
                n_components=self.n_regimes,
                covariance_type="diag",
                n_iter=200,
                random_state=42,
                tol=1e-4,
            )
        except ImportError:
            logger.warning("hmmlearn not available, using clustering fallback")
            self._hmm = None

    def _compute_state_features(self, df: pd.DataFrame) -> np.ndarray:
        """Compute features for regime detection: returns + volatility."""
        close = df["close"].values.astype(float)
        returns = np.diff(np.log(close))
        returns = np.insert(returns, 0, 0.0)

        # Volatility: rolling std of returns
        vol = pd.Series(returns).rolling(self.volatility_window).std().fillna(0).values

        # Trend strength: abs(MA slope)
        ma_slope = np.zeros(len(close))
        if len(close) > self.trend_window:
            ma = pd.Series(close).rolling(self.trend_window).mean().values
            ma_slope[self.trend_window:] = (
                ma[self.trend_window:] - ma[:-self.trend_window]
            ) / close[self.trend_window:]

        # Stack features
        X = np.column_stack([
            returns,
            vol,
            np.abs(ma_slope),
        ])
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        return X

    def detect(self, df: pd.DataFrame) -> Regime:
        """Detect the current market regime.

        Args:
            df: OHLCV DataFrame

        Returns:
            Regime enum value for the current bar
        """
        if len(df) < max(self.hmm_lookback, 50):
            return Regime.UNKNOWN

        use_df = df.iloc[-self.hmm_lookback:]
        X = self._compute_state_features(use_df)

        try:
            # Try HMM first
            if self._hmm is not None:
                try:
                    if not self._hmm_fitted:
                        # Scale features for HMM stability
                        X_std = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
                        self._hmm.fit(X_std)
                        self._hmm_fitted = True
                        self._map_regimes(X, X_std)

                    # Get latest state
                    X_latest = X[-1:].copy()
                    X_std_latest = (X_latest - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
                    state = self._hmm.predict(X_std_latest)[0]
                    return self._regime_map.get(state, Regime.UNKNOWN)

                except Exception as e:
                    logger.debug(f"HMM detection failed: {e}, falling back to clustering")

            # Fallback: heuristic clustering
            return self._heuristic_regime(use_df)
        except Exception:
            return Regime.UNKNOWN

    def _map_regimes(self, X: np.ndarray, X_std: np.ndarray):
        """Map HMM states to Regime enum based on feature statistics."""
        if not self._hmm_fitted:
            return

        states = self._hmm.predict(X_std)
        state_stats = {}
        for s in range(self.n_regimes):
            mask = states == s
            if mask.sum() < 5:
                continue
            # Mean volatility and return for this state
            state_vol = X[mask, 1].mean()
            state_ret = X[mask, 0].mean()
            state_trend = X[mask, 2].mean()
            state_stats[s] = (state_vol, state_ret, state_trend)

        if not state_stats:
            return

        # Sort states by volatility
        sorted_by_vol = sorted(state_stats.items(), key=lambda x: x[1][0])
        if len(sorted_by_vol) >= 3:
            low_vol_s = sorted_by_vol[0][0]
            mid_low_s = sorted_by_vol[1][0] if len(sorted_by_vol) > 2 else sorted_by_vol[0][0]
            high_vol_s = sorted_by_vol[-1][0]

            self._regime_map[low_vol_s] = Regime.LOW_VOL
            self._regime_map[high_vol_s] = Regime.HIGH_VOL

            # Mid-vol states: check trend
            for s, (vol, ret, trend) in sorted_by_vol[1:-1]:
                if abs(trend) > 0.001:
                    self._regime_map[s] = Regime.TRENDING
                else:
                    self._regime_map[s] = Regime.RANGING

            # Map remaining
            for s in state_stats:
                if s not in self._regime_map:
                    self._regime_map[s] = Regime.RANGING
        else:
            # Few states: simple mapping
            for s, (vol, ret, trend) in sorted_by_vol:
                if vol > X[:, 1].mean() * 1.5:
                    self._regime_map[s] = Regime.HIGH_VOL
                elif abs(trend) > 0.001:
                    self._regime_map[s] = Regime.TRENDING
                else:
                    self._regime_map[s] = Regime.RANGING

        logger.debug(f"HMM regime map: {self._regime_map}")

    def _heuristic_regime(self, df: pd.DataFrame) -> Regime:
        """Fallback: heuristic regime classification."""
        close = df["close"].values.astype(float)
        returns = np.diff(np.log(close))
        returns = np.insert(returns, 0, 0.0)

        # Current volatility vs historical
        vol = pd.Series(returns).rolling(self.volatility_window).std().values
        current_vol = vol[-1]
        median_vol = np.nanmedian(vol[-self.hmm_lookback:])

        # Trend strength
        ma_short = pd.Series(close).rolling(20).mean().values
        ma_long = pd.Series(close).rolling(50).mean().values
        trend_strength = abs(ma_short[-1] - ma_long[-1]) / close[-1]
        slope = ma_short[-1] - ma_short[-6]

        # Classify
        if current_vol > median_vol * 1.5:
            return Regime.HIGH_VOL
        elif trend_strength > 0.02 or (abs(slope) / close[-1] > 0.005):
            return Regime.TRENDING
        elif current_vol < median_vol * 0.5:
            return Regime.LOW_VOL
        else:
            return Regime.RANGING

    def refit(self, df: pd.DataFrame):
        """Force refit of the regime detector."""
        self._hmm_fitted = False
        self._regime_map.clear()


# ---------------------------------------------------------------------------
# RegimeSwitch Strategy
# ---------------------------------------------------------------------------

class RegimeSwitchStrategy(BaseStrategy):
    """Regime-Switching Strategy — adapts to market conditions.

    Uses HMM or clustering to detect the current market regime and
    activates the appropriate sub-strategy.

    Parameters
    ----------
    symbols : list[str]
    timeframes : list[str]
    regime_lookback : int
        Bars used for regime detection (default 100)
    retrain_regime_every : int
        Bars between regime model refits (default 50)
    regime_change_cooldown : int
        Minimum bars before switching regime action
    vol_window : int
        Volatility estimation window
    trend_window : int
        Trend detection window

    Sub-strategy params:
    trend_ema_period : int
    trend_sl_pct : float
    trend_tp_pct : float
    mr_rsi_period : int
    mr_oversold : int
    mr_overbought : int
    mr_sl_pct : float
    mr_tp_pct : float
    cooldown_bars : int
    leverage : int
    high_vol_capital_pct : float (0.0-1.0, how much capital to use in high vol)
    """

    def __init__(
        self,
        name: str = "RegimeSwitch",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        regime_lookback: int = 100,
        retrain_regime_every: int = 50,
        regime_change_cooldown: int = 3,
        vol_window: int = 20,
        trend_window: int = 50,
        # Trend sub-strategy
        trend_ema_period: int = 50,
        trend_sl_pct: float = 0.02,
        trend_tp_pct: float = 0.05,
        # Mean reversion sub-strategy
        mr_rsi_period: int = 14,
        mr_oversold: int = 30,
        mr_overbought: int = 70,
        mr_sl_pct: float = 0.02,
        mr_tp_pct: float = 0.04,
        # Risk
        cooldown_bars: int = 5,
        leverage: int = 3,
        high_vol_capital_pct: float = 0.25,  # reduce position in high vol
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params = {
            "regime_lookback": regime_lookback,
            "retrain_regime_every": retrain_regime_every,
            "regime_change_cooldown": regime_change_cooldown,
            "vol_window": vol_window,
            "trend_window": trend_window,
            "trend_ema_period": trend_ema_period,
            "trend_sl_pct": trend_sl_pct,
            "trend_tp_pct": trend_tp_pct,
            "mr_rsi_period": mr_rsi_period,
            "mr_oversold": mr_oversold,
            "mr_overbought": mr_overbought,
            "mr_sl_pct": mr_sl_pct,
            "mr_tp_pct": mr_tp_pct,
            "cooldown_bars": cooldown_bars,
            "leverage": leverage,
            "high_vol_capital_pct": high_vol_capital_pct,
        }

        # Per-symbol state
        self._detectors: Dict[str, RegimeDetector] = {}
        self._current_regime: Dict[str, Regime] = {}
        self._prev_regime: Dict[str, Regime] = {}
        self._bars_since_regime_change: Dict[str, int] = {}
        self._regime_stats: Dict[str, List[RegimeStats]] = {}
        self._bars_since_last_trade = cooldown_bars + 1

    def _get_detector(self, symbol: str) -> RegimeDetector:
        """Get or create a regime detector for a symbol."""
        if symbol not in self._detectors:
            self._detectors[symbol] = RegimeDetector(
                n_regimes=4,
                hmm_lookback=self.params["regime_lookback"],
                volatility_window=self.params["vol_window"],
                trend_window=self.params["trend_window"],
            )
        return self._detectors[symbol]

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute regime detection indicators."""
        key = (symbol, timeframe)
        p = self.params

        # Store RSI, EMA, ATR for sub-strategies
        ind = pd.DataFrame(index=df.index)
        close = df["close"]

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(span=p["mr_rsi_period"], adjust=False).mean()
        avg_loss = loss.ewm(span=p["mr_rsi_period"], adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        ind["rsi"] = 100 - (100 / (1 + rs))

        # EMA
        ind["ema"] = close.ewm(span=p["trend_ema_period"], adjust=False).mean()
        ind["ema_slope"] = ind["ema"].diff(5)

        # Volatility (20-bar rolling)
        returns = np.log(close / close.shift(1))
        ind["volatility"] = returns.rolling(p["vol_window"]).std()

        # ATR
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift(1)).abs()
        low_close = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=14, adjust=False).mean()
        ind["close"] = close
        ind["high"] = df["high"]
        ind["low"] = df["low"]
        ind["volume"] = df.get("volume", pd.Series(1.0, index=df.index))

        self._indicators[key] = ind

        # Detect regime periodically
        detector = self._get_detector(symbol)
        self._bars_since_regime_change[symbol] = (
            self._bars_since_regime_change.get(symbol, 0) + 1
        )

        if self._bars_since_regime_change[symbol] >= p["retrain_regime_every"]:
            detector.refit(df)

        regime = detector.detect(df)
        prev = self._current_regime.get(symbol)

        if regime != prev and prev is not None:
            self._prev_regime[symbol] = prev
            self._bars_since_regime_change[symbol] = 0
            logger.info(
                f"RegimeSwitch [{symbol}]: {prev.value} -> {regime.value}"
            )
        elif prev is None:
            self._bars_since_regime_change[symbol] = 0

        self._current_regime[symbol] = regime

    def generate_signal(self, symbol: str) -> Signal:
        """Generate signal based on current regime and sub-strategy."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)
        df = self._data.get(key)
        p = self.params

        if ind is None or df is None or len(df) < 100:
            return Signal(
                SignalType.HOLD, symbol,
                reason="Warming up", strategy_name=self.name,
            )

        self._bars_since_last_trade += 1
        regime = self._current_regime.get(symbol, Regime.UNKNOWN)

        has_pos = self.has_position(symbol)

        # --- Position management ---
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]
            price = float(df["close"].iloc[-1])

            # Exit if regime changed to HIGH_VOL (reduce risk)
            if regime == Regime.HIGH_VOL:
                sig_type = (
                    SignalType.CLOSE_LONG if pos["side"] == "LONG"
                    else SignalType.CLOSE_SHORT
                )
                return Signal(
                    sig_type, symbol, price=price,
                    reason=f"Regime changed to HIGH_VOL — reducing risk",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

            # Exit if regime opposes position
            if pos["side"] == "LONG" and regime == Regime.RANGING:
                # In ranging market, take profits early
                atr = float(ind["atr"].iloc[-1]) if not pd.isna(ind["atr"].iloc[-1]) else 0
                if atr > 0 and price >= entry + atr * 2:
                    return Signal(
                        SignalType.CLOSE_LONG, symbol, price=price,
                        reason="Ranging regime — taking profit",
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    )

            # Standard SL/TP checks on ATR
            atr = float(ind["atr"].iloc[-1]) if not pd.isna(ind["atr"].iloc[-1]) else 0
            sl_mult = p["trend_sl_pct"] * entry / max(atr, 1e-9) if atr > 0 else 1.0
            tp_mult = p["trend_tp_pct"] * entry / max(atr, 1e-9) if atr > 0 else 1.0

            if pos["side"] == "LONG":
                if atr > 0 and price <= entry - max(atr * sl_mult, entry * p["trend_sl_pct"]):
                    return Signal(
                        SignalType.CLOSE_LONG, symbol, price=price,
                        reason=f"SL -{p['trend_sl_pct']*100:.1f}%",
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    )
                if atr > 0 and price >= entry + max(atr * tp_mult, entry * p["trend_tp_pct"]):
                    return Signal(
                        SignalType.CLOSE_LONG, symbol, price=price,
                        reason=f"TP +{p['trend_tp_pct']*100:.1f}%",
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    )
            else:
                if atr > 0 and price >= entry + max(atr * sl_mult, entry * p["trend_sl_pct"]):
                    return Signal(
                        SignalType.CLOSE_SHORT, symbol, price=price,
                        reason=f"SL -{p['trend_sl_pct']*100:.1f}%",
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    )
                if atr > 0 and price <= entry - max(atr * tp_mult, entry * p["trend_tp_pct"]):
                    return Signal(
                        SignalType.CLOSE_SHORT, symbol, price=price,
                        reason=f"TP +{p['trend_tp_pct']*100:.1f}%",
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    )

            return Signal(
                SignalType.HOLD, symbol,
                reason=f"Holding in {regime.value}",
                strategy_name=self.name,
                timestamp=df.index[-1],
            )

        # --- Entry: dispatch to sub-strategy ---
        if self._bars_since_last_trade <= p["cooldown_bars"]:
            return Signal(
                SignalType.HOLD, symbol,
                reason="Cooldown", strategy_name=self.name,
                timestamp=df.index[-1],
            )

        if self._bars_since_regime_change.get(symbol, 0) < p["regime_change_cooldown"]:
            return Signal(
                SignalType.HOLD, symbol,
                reason=f"Regime change cooldown ({regime.value})",
                strategy_name=self.name,
                timestamp=df.index[-1],
            )

        # Dispatch based on regime
        if regime == Regime.TRENDING:
            signal = _trend_follow_signal(
                df,
                ema_period=p["trend_ema_period"],
                sl_pct=p["trend_sl_pct"],
                tp_pct=p["trend_tp_pct"],
                cooldown_bars=0,  # handled by outer cooldown
                leverage=p["leverage"],
                strategy_name=self.name,
            )

        elif regime == Regime.RANGING:
            signal = _mean_reversion_signal(
                df,
                rsi_period=p["mr_rsi_period"],
                oversold=p["mr_oversold"],
                overbought=p["mr_overbought"],
                sl_pct=p["mr_sl_pct"],
                tp_pct=p["mr_tp_pct"],
                leverage=p["leverage"],
                strategy_name=self.name,
            )

        elif regime == Regime.LOW_VOL:
            # Low vol: use mean reversion with tighter stops
            signal = _mean_reversion_signal(
                df,
                rsi_period=p["mr_rsi_period"],
                oversold=p["mr_oversold"] - 5,
                overbought=p["mr_overbought"] + 5,
                sl_pct=p["mr_sl_pct"] * 0.75,
                tp_pct=p["mr_tp_pct"] * 0.75,
                leverage=p["leverage"],
                strategy_name=self.name,
            )

        elif regime == Regime.HIGH_VOL:
            # High vol: stay flat or use reduced size
            # Only trade if extremely confident
            return Signal(
                SignalType.HOLD, symbol,
                reason=f"HIGH_VOL regime — staying flat",
                strategy_name=self.name,
                timestamp=df.index[-1],
            )

        else:  # UNKNOWN
            return Signal(
                SignalType.HOLD, symbol,
                reason="Unknown regime",
                strategy_name=self.name,
                timestamp=df.index[-1],
            )

        # Update symbol in returned signal and add regime info
        reason = f"[{regime.value}] {signal.reason}"
        self._bars_since_last_trade = 0

        return Signal(
            type=signal.type,
            symbol=symbol,
            price=signal.price,
            quantity=signal.quantity * p["high_vol_capital_pct"] if regime == Regime.HIGH_VOL else signal.quantity,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            reason=reason,
            confidence=signal.confidence,
            leverage=p["leverage"],
            strategy_name=self.name,
            timestamp=df.index[-1],
        )

    def get_regime(self, symbol: str) -> Regime:
        """Get the current regime for a symbol."""
        return self._current_regime.get(symbol, Regime.UNKNOWN)

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["regime_lookback"], 200),
        }
