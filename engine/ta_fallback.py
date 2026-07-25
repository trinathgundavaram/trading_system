"""Drop-in replacement for pandas_ta's `.ta` DataFrame accessor, used only if the
real pandas_ta package can't be imported.

Why this exists: as of this build, `pandas-ta==0.3.14b0` (the version pinned in
requirements.txt) has been pulled from PyPI, and the only versions PyPI still
serves (0.4.67b0+) use Python 3.12-only f-string syntax - they raise a
SyntaxError on import under Python 3.10/3.11. So on any machine not yet on
Python 3.12, `pip install pandas-ta` either fails outright or installs
something that can't be imported. Rather than let that take down the whole
platform at startup, engine/ticker_analyzer.py tries the real package first and
falls back to this hand-rolled accessor (same method names, same output column
names as pandas_ta) if it's unavailable.

If you're on Python 3.12+, `pip install pandas-ta` may work fine and this file
is simply never used (see the try/except at the top of ticker_analyzer.py).
"""
import numpy as np
import pandas as pd


@pd.api.extensions.register_dataframe_accessor("ta")
class _FallbackTA:
    def __init__(self, df):
        self._df = df

    def sma(self, length: int = 20, append: bool = True):
        s = self._df["close"].rolling(length).mean()
        if append:
            self._df[f"SMA_{length}"] = s
        return s

    def ema(self, length: int = 20, append: bool = True):
        s = self._df["close"].ewm(span=length, adjust=False).mean()
        if append:
            self._df[f"EMA_{length}"] = s
        return s

    def rsi(self, length: int = 14, append: bool = True):
        delta = self._df["close"].diff()
        gain = delta.clip(lower=0).rolling(length).mean()
        loss = (-delta.clip(upper=0)).rolling(length).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        if append:
            self._df[f"RSI_{length}"] = rsi
        return rsi

    def stoch(self, k: int = 14, d: int = 3, smooth_k: int = 3, append: bool = True):
        low_min = self._df["low"].rolling(k).min()
        high_max = self._df["high"].rolling(k).max()
        denom = (high_max - low_min).replace(0, np.nan)
        raw_k = 100 * (self._df["close"] - low_min) / denom
        k_smooth = raw_k.rolling(smooth_k).mean()
        d_smooth = k_smooth.rolling(d).mean()
        if append:
            self._df[f"STOCHk_{k}_{d}_{smooth_k}"] = k_smooth
            self._df[f"STOCHd_{k}_{d}_{smooth_k}"] = d_smooth
        return k_smooth, d_smooth

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9, append: bool = True):
        ema_fast = self._df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = self._df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line
        if append:
            self._df[f"MACD_{fast}_{slow}_{signal}"] = macd_line
            self._df[f"MACDs_{fast}_{slow}_{signal}"] = signal_line
            self._df[f"MACDh_{fast}_{slow}_{signal}"] = hist
        return macd_line, signal_line, hist

    def bbands(self, length: int = 20, std: float = 2, append: bool = True):
        mid = self._df["close"].rolling(length).mean()
        sd = self._df["close"].rolling(length).std()
        upper = mid + std * sd
        lower = mid - std * sd
        if append:
            self._df[f"BBU_{length}_{std}.0" if isinstance(std, int) else f"BBU_{length}_{std}"] = upper
            self._df[f"BBL_{length}_{std}.0" if isinstance(std, int) else f"BBL_{length}_{std}"] = lower
        return upper, lower

    def atr(self, length: int = 14, append: bool = True):
        high, low, close = self._df["high"], self._df["low"], self._df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_val = tr.rolling(length).mean()
        if append:
            self._df[f"ATRr_{length}"] = atr_val
        return atr_val

    def obv(self, append: bool = True):
        direction = np.sign(self._df["close"].diff().fillna(0))
        obv_val = (direction * self._df["volume"]).cumsum()
        if append:
            self._df["OBV"] = obv_val
        return obv_val

    def vwap(self, append: bool = True):
        typical = (self._df["high"] + self._df["low"] + self._df["close"]) / 3
        cum_vol = self._df["volume"].cumsum()
        cum_vol_price = (typical * self._df["volume"]).cumsum()
        vwap_val = cum_vol_price / cum_vol.replace(0, np.nan)
        if append:
            self._df["VWAP_D"] = vwap_val
        return vwap_val

    def adx(self, length: int = 14, append: bool = True):
        """Wilder's ADX/+DI/-DI. Column names (ADX_14/DMP_14/DMN_14) match
        real pandas_ta's naming exactly so engine/ticker_analyzer.py can read
        them the same way regardless of which backend computed them."""
        high, low, close = self._df["high"], self._df["low"], self._df["close"]
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=self._df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=self._df.index)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / length, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx_val = dx.ewm(alpha=1 / length, adjust=False).mean()
        if append:
            self._df[f"ADX_{length}"] = adx_val
            self._df[f"DMP_{length}"] = plus_di
            self._df[f"DMN_{length}"] = minus_di
        return adx_val

    def cmf(self, length: int = 20, append: bool = True):
        """Chaikin Money Flow. Column name (CMF_20) matches real pandas_ta."""
        high, low, close, volume = self._df["high"], self._df["low"], self._df["close"], self._df["volume"]
        money_flow_mult = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
        money_flow_vol = money_flow_mult * volume
        cmf_val = money_flow_vol.rolling(length).sum() / volume.rolling(length).sum().replace(0, np.nan)
        if append:
            self._df[f"CMF_{length}"] = cmf_val
        return cmf_val
