"""
quantflow_scanner_core.py
─────────────────────────
Headless scan pipeline for QuantFlow.  No UI framework dependency — safe to
run in GitHub Actions, cron jobs, or any non-browser context.  Reads combo
definitions from quantflow_combos.py but performs ALL classification locally
so the level-logic version is fixed here.

Public API
----------
fetch_universe(min_volume_usdt, top_n)  → list[str]
fetch_candles(symbol, interval, limit)  → pd.DataFrame
btc_regime()                            → str   ('BULL'|'BEAR'|'CHOP'|'UNKNOWN')
score_signal(symbol, timeframe, dir, bar_offset) → dict | None
get_matching_combos(sig, enabled, btc_regime, allowed_levels) → list[dict]

Constants
---------
LEVELS, LEVEL_SETTINGS, BODY_DEAD_ZONE_MIN/MAX, BODY_FLOOR_TREND/CT/CEIL,
VOL_FLOOR, ADX_FLOOR, ADX_CEIL
"""

from __future__ import annotations

import datetime
import logging
import os
from datetime import timedelta

import numpy as np
import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# ─── quantflow_combos import (data only) ─────────────────────────────────────
try:
    import quantflow_combos as _qfcombos  # noqa: F401
    _QFCOMBOS_OK = True
except ImportError:
    _qfcombos = None
    _QFCOMBOS_OK = False
    log.warning("quantflow_combos not importable — get_matching_combos will return []")

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS  (verbatim from app.py)
# ═══════════════════════════════════════════════════════════════════════════════

LEVELS = ('STRICT', 'RELAXED', 'LOOSE')  # ordered strictest first

LEVEL_SETTINGS = {
    'STRICT': {
        'body_pad': 0.00, 'vol_pad_min': 0.00, 'vol_pad_max': 0.00,
        'adx_pad': 0,    'size_factor': 1.00, 'pf_haircut': 1.00,
    },
    'RELAXED': {
        'body_pad': 0.03, 'vol_pad_min': 0.15, 'vol_pad_max': 0.50,
        'adx_pad': 2,    'size_factor': 0.75, 'pf_haircut': 0.92,
    },
    'LOOSE': {
        'body_pad': 0.05, 'vol_pad_min': 0.30, 'vol_pad_max': 1.00,
        'adx_pad': 3,    'size_factor': 0.50, 'pf_haircut': 0.80,
    },
}

# Hard floors / caps — cannot be crossed regardless of level
BODY_DEAD_ZONE_MIN = 0.60
BODY_DEAD_ZONE_MAX = 0.70
BODY_FLOOR_TREND   = 0.40
BODY_FLOOR_CT      = 0.78
BODY_CEIL          = 1.01   # 1.01 preserves CT4/CT6 strict band convention
VOL_FLOOR          = 1.20
ADX_FLOOR          = 25.0
ADX_CEIL           = 50.0

_SCANNER_EXCLUDE = {
    'USDT', 'BUSD', 'USDC', 'TUSD', 'DAI', 'FDUSD', 'USDP', 'USDD',
    'PYUSD', 'AEUR', 'EURI',
    'WBTC', 'WETH', 'WBETH',
}

_SCORE_WEIGHTS = {
    'body':    25,
    'volume':  20,
    'adx':     20,
    'regime':  25,
    'recency': 10,
}

# Accept both upper and lower case interval keys
_BINANCE_INTERVAL = {
    '1d': '1d', '4h': '4h', '1h': '1h', '1w': '1w',
    '1D': '1d', '4H': '4h', '1H': '1h', '1W': '1w',
}

_BINANCE_KLINES_URLS = [
    ('https://api.binance.com/api/v3/klines',          True),
    ('https://data-api.binance.vision/api/v3/klines',  False),
]


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS — combo level logic  (ported from app.py _qf_* verbatim)
# ═══════════════════════════════════════════════════════════════════════════════

def _widen_criteria(combo: dict, level: str) -> dict:
    """
    Return a copy of combo['criteria'] with bounds widened per level,
    respecting all dead-zone and hard-cap rules.

    Body widening is asymmetric per combo type:
      - trend with body_max <= 0.60 (e.g. C1A 0.5-0.6): widen DOWN only.
      - trend with body_min >= 0.70 (e.g. C6A 0.7-0.8): widen UP only.
      - countertrend (body 0.80-1.00): widens up to 1.00, down to 0.78.
    Volume widens symmetrically with floor 1.20.
    ADX widens symmetrically (trend only) with floor 25 / ceil 50.
    """
    crit = dict(combo['criteria'])
    if level == 'STRICT':
        return crit

    s = LEVEL_SETTINGS[level]
    combo_type  = combo.get('combo_type', 'trend_following')
    body_min_o  = float(crit['body_min'])
    body_max_o  = float(crit['body_max'])
    vol_min_o   = float(crit['vol_min'])
    vol_max_o   = float(crit['vol_max'])

    # Body widening — asymmetric for trend, symmetric for CT
    if combo_type == 'trend_following':
        if body_max_o <= BODY_DEAD_ZONE_MIN + 1e-9:
            # Band is entirely below dead zone → widen DOWN only; cap at dead zone
            crit['body_min'] = max(BODY_FLOOR_TREND, body_min_o - s['body_pad'])
            crit['body_max'] = min(body_max_o, BODY_DEAD_ZONE_MIN)
        elif body_min_o >= BODY_DEAD_ZONE_MAX - 1e-9:
            # Band is entirely above dead zone → widen UP only; floor at dead zone
            crit['body_min'] = max(body_min_o, BODY_DEAD_ZONE_MAX)
            crit['body_max'] = min(BODY_CEIL, body_max_o + s['body_pad'])
        else:
            # Straddles dead zone (shouldn't exist in current combo set)
            crit['body_min'] = max(BODY_FLOOR_TREND, body_min_o - s['body_pad'])
            crit['body_max'] = min(BODY_CEIL, body_max_o + s['body_pad'])
    else:  # countertrend
        crit['body_min'] = max(BODY_FLOOR_CT, body_min_o - s['body_pad'])
        crit['body_max'] = min(BODY_CEIL,     body_max_o + s['body_pad'])

    # Volume widening — symmetric, with floor
    crit['vol_min'] = max(VOL_FLOOR, vol_min_o - s['vol_pad_min'])
    crit['vol_max'] = vol_max_o + s['vol_pad_max']

    # ADX widening — trend only (countertrend leaves its 0-999 range alone)
    if combo_type == 'trend_following':
        crit['adx_min'] = max(ADX_FLOOR, float(crit['adx_min']) - s['adx_pad'])
        crit['adx_max'] = min(ADX_CEIL,  float(crit['adx_max']) + s['adx_pad'])

    return crit


def _is_regime_aligned(direction: str, btc_regime: str) -> bool:
    """True when trade direction agrees with BTC regime."""
    if btc_regime == 'BULL':
        return direction == 'long'
    if btc_regime == 'BEAR':
        return direction == 'short'
    return False  # CHOP / UNKNOWN → aligned combos can't classify


def _signal_matches_at_level(sig: dict, combo: dict,
                              btc_regime_val: str, level: str) -> bool:
    """Per-level matcher: does sig satisfy combo's (level-widened) criteria?"""
    crit       = _widen_criteria(combo, level)
    combo_type = combo.get('combo_type', 'trend_following')

    tf_norm = (sig.get('timeframe', '') or '').lower()
    if tf_norm not in combo['tf_eligible']:
        return False

    sig_dir = sig.get('direction', '')
    if combo_type == 'countertrend':
        # signal_direction_required can be:
        #   - a string ('long' or 'short') for the 17 individual CT combos,
        #     which require the candle direction to match exactly
        #   - None for the unified TIER_3 synth combo, meaning "accept both
        #     directions, the tier's bands are direction-agnostic"
        # The previous check `sig_dir != crit.get('signal_direction_required', '')`
        # treated None as a value that no string equals, silently rejecting every
        # signal — caused TIER_3 to never match in the worker either.
        sdr = crit.get('signal_direction_required')
        if sdr is not None and sig_dir != sdr:
            return False
    else:
        if sig_dir not in crit['directions']:
            return False

    try:
        body_abs = abs(float(sig.get('body_pct', 0)))
        vol_mult = float(sig.get('vol_mult', 0))
        adx      = float(sig.get('adx', 0))
    except (TypeError, ValueError):
        return False

    if not (crit['body_min'] <= body_abs  < crit['body_max']):  return False
    if not (crit['vol_min']  <= vol_mult  < crit['vol_max']):   return False
    if not (crit['adx_min']  <= adx       < crit['adx_max']):   return False

    if crit['regime_mode'] == 'A':
        if btc_regime_val is None:
            return False
        if not _is_regime_aligned(sig['direction'], btc_regime_val):
            return False
    return True


def _classify_signal_level(sig: dict, combo: dict,
                             btc_regime_val: str = None,
                             allowed_levels: tuple = ('STRICT',)):
    """Walk levels strictest-first → first match in allowed_levels, or None."""
    for lvl in LEVELS:
        if lvl not in allowed_levels:
            continue
        if _signal_matches_at_level(sig, combo, btc_regime_val, lvl):
            return lvl
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PRIVATE HELPERS — data & indicators
# ═══════════════════════════════════════════════════════════════════════════════

def _get_session(hour_wib: int) -> str:
    """Return trading session name for a given WIB hour (0-23)."""
    if hour_wib >= 20:
        return 'NY+London'
    elif 15 <= hour_wib < 20:
        return 'London'
    elif 7 <= hour_wib < 15:
        return 'Asian'
    else:
        return 'Dead Zone'


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flatten MultiIndex, lowercase columns, keep OHLCV, compute all derived cols.
    Adds: body, candle_range, body_pct (fraction 0-1), vol_avg_7, vol_mult,
          atr14, atr_ratio, vol_delta_5, ema5/15/21, candle_rank_20,
          vol_rank_20, vol_delta_20, vol_delta_regime, body_vs_atr,
          dist_from_ema21_pct.
    """
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    missing = [c for c in ['open', 'high', 'low', 'close', 'volume']
               if c not in df.columns]
    if missing:
        log.warning('_clean_df: missing columns %s', missing)
        return pd.DataFrame()
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df.dropna(inplace=True)

    df['body']         = df['close'] - df['open']
    df['candle_range'] = df['high']  - df['low']
    cr = df['candle_range'].copy()
    cr[cr == 0] = float('nan')
    df['body_pct']  = df['body'] / cr          # fraction, signed (–1 to +1)
    df['vol_avg_7'] = df['volume'].shift(1).rolling(7).mean()
    df['vol_mult']  = df['volume'] / df['vol_avg_7']

    # ATR(14)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low']  - df['close'].shift()).abs(),
    ], axis=1).max(axis=1)
    df['atr14']    = tr.rolling(14).mean()
    df['atr_ratio'] = df['atr14'] / df['atr14'].rolling(20).mean()

    # Volume delta
    close_pos     = (df['close'] - df['low']) / cr
    vol_delta     = df['volume'] * (2 * close_pos - 1)
    df['vol_delta_5']  = vol_delta.rolling(5).sum()
    df['vol_delta_20'] = vol_delta.rolling(20).sum()
    _vd5_mean = df['vol_delta_5'].rolling(20).mean()
    _vd5_std  = df['vol_delta_5'].rolling(20).std().replace(0, float('nan'))
    df['vol_delta_regime'] = (df['vol_delta_5'] - _vd5_mean) / _vd5_std

    # EMA stack (shift(1) avoids lookahead bias)
    df['ema5']  = df['close'].shift(1).ewm(span=5,  adjust=False).mean()
    df['ema15'] = df['close'].shift(1).ewm(span=15, adjust=False).mean()
    df['ema21'] = df['close'].shift(1).ewm(span=21, adjust=False).mean()

    # Percentile ranks
    df['candle_rank_20'] = df['body_pct'].abs().rolling(20).rank(pct=True)
    df['vol_rank_20']    = df['volume'].rolling(20).rank(pct=True)

    # Engineered features
    df['body_vs_atr']         = df['body'].abs() / df['atr14'].replace(0, float('nan'))
    df['dist_from_ema21_pct'] = ((df['close'] - df['ema21']) / df['ema21']) * 100
    return df


def _calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Calculate ADX, DI+, DI− from OHLCV DataFrame.
    Returns DataFrame with columns adx, di_plus, di_minus aligned to df.index.
    """
    high  = df['high']
    low   = df['low']
    close = df['close']

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    up   = high - high.shift(1)
    down = low.shift(1) - low

    dm_plus  = pd.Series(np.where((up > down) & (up > 0),   up,   0.0), index=df.index)
    dm_minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)

    atr_w    = tr.ewm(alpha=1 / period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm( alpha=1 / period, adjust=False).mean() / atr_w
    di_minus = 100 * dm_minus.ewm(alpha=1 / period, adjust=False).mean() / atr_w

    dx  = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, float('nan'))
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    return pd.DataFrame({'adx': adx, 'di_plus': di_plus, 'di_minus': di_minus},
                        index=df.index)


def _calculate_regime_score(df, bar_index, direction, adx_df,
                             timeframe='1D', ticker='') -> dict:
    """
    Compute a 0-100 regime score from ADX, ATR ratio, EMA alignment,
    session, DI gap, and volume delta.
    Returns dict with: score, verdict (GREEN/YELLOW/RED), breakdown_line,
    flip_condition, hard_overrides.
    """
    is_daily  = timeframe.upper() in ('1D', '1W')
    is_crypto = str(ticker).upper().endswith('USDT')

    try:
        bar = df.iloc[bar_index]
    except (IndexError, TypeError):
        bar = df.iloc[-1]

    close      = float(bar.get('close',     0))
    atr        = float(bar.get('atr14',     0) or 0)
    atr_ratio  = float(bar.get('atr_ratio', 1) or 1)
    ema5       = float(bar.get('ema5',      close) or close)
    ema15      = float(bar.get('ema15',     close) or close)
    ema21      = float(bar.get('ema21',     close) or close)
    vol_delta5 = float(bar.get('vol_delta_5', 0) or 0)
    bar_ts     = df.index[bar_index] if bar_index < len(df) else df.index[-1]

    adx_val  = float(adx_df['adx'].iloc[bar_index])      if adx_df is not None and 'adx'      in adx_df.columns else 0.0
    di_plus  = float(adx_df['di_plus'].iloc[bar_index])  if adx_df is not None and 'di_plus'  in adx_df.columns else 0.0
    di_minus = float(adx_df['di_minus'].iloc[bar_index]) if adx_df is not None and 'di_minus' in adx_df.columns else 0.0

    adx_3ago = 0.0
    if adx_df is not None and 'adx' in adx_df.columns and bar_index >= 3:
        adx_3ago = float(adx_df['adx'].iloc[bar_index - 3])

    atr_ratio_10ago = 1.0
    if bar_index >= 10 and 'atr_ratio' in df.columns:
        atr_ratio_10ago = float(df['atr_ratio'].iloc[bar_index - 10] or 1)

    atr_high_streak = 0
    if 'atr_ratio' in df.columns and bar_index >= 10:
        atr_high_streak = int(
            (df['atr_ratio'].iloc[max(0, bar_index - 10):bar_index + 1] > 1.5).sum()
        )

    # ── 1. ADX score (0-30) ───────────────────────────────────────────────────
    if adx_val < 15:
        adx_pts = 0
    elif adx_val < 20:
        adx_pts = 8
    elif adx_val < 25:
        adx_pts = 18
    elif adx_val < 30:
        adx_pts = 28
    elif adx_val <= 40:
        adx_pts = 30
    else:
        adx_pts = 25  # overheated penalty

    adx_declining = adx_val > 25 and adx_3ago > 0 and adx_val < adx_3ago
    if adx_declining:
        adx_pts -= 5

    adx_max = 30

    # ── 2. ATR ratio score (0-25) ─────────────────────────────────────────────
    if atr_ratio < 0.6:
        atr_pts = 5
    elif atr_ratio < 0.8:
        atr_pts = 12
    elif atr_ratio < 1.0:
        atr_pts = 18
    elif atr_ratio < 1.5:
        atr_pts = 25
    elif atr_ratio < 2.0:
        atr_pts = 20
    else:
        atr_pts = 10

    if atr_ratio > 1.0 and atr_ratio_10ago < 0.8:
        atr_pts = min(25, atr_pts + 5)
    if atr_high_streak >= 10:
        atr_pts = max(0, atr_pts - 5)

    atr_max = 25

    # ── 3. EMA alignment score (0-25) ────────────────────────────────────────
    if direction == 'long':
        stack_full    = ema5 > ema15 and ema15 > ema21
        stack_partial = (ema5 > ema15) or (ema15 > ema21)
    else:
        stack_full    = ema5 < ema15 and ema15 < ema21
        stack_partial = (ema5 < ema15) or (ema15 < ema21)

    stack_pts = 10 if stack_full else (5 if stack_partial else 0)
    htf_score = 5   # neutral — no HTF data in headless mode
    cross_tf_pts = 5 if (stack_pts >= 5 and htf_score >= 5) else 0
    ema_pts = min(25, stack_pts + cross_tf_pts)
    ema_max = 25

    # ── 4. Session score (0-15) ───────────────────────────────────────────────
    sess_pts = 0
    sess_max = 15
    if is_daily:
        sess_pts = 0
        adx_max  = 35
        atr_max  = 30
        ema_max  = 30
        adx_pts  = min(adx_max, adx_pts)
        atr_pts  = min(atr_max, atr_pts)
        ema_pts  = min(ema_max, ema_pts)
    else:
        try:
            if hasattr(bar_ts, 'to_pydatetime'):
                _naive = bar_ts.to_pydatetime()
            else:
                _naive = bar_ts
            wib_hour = (_naive.hour + 7) % 24
        except Exception:
            wib_hour = 12
        sess_name = _get_session(wib_hour)
        if is_crypto:
            sess_pts = 7 if sess_name == 'Dead Zone' else 10
        else:
            _sess_map = {'NY+London': 15, 'London': 13, 'Asian': 4, 'Dead Zone': 2}
            sess_pts  = _sess_map.get(sess_name, 4)

    # ── 5. DI gap score (0-5) ─────────────────────────────────────────────────
    di_gap = di_plus - di_minus
    if direction == 'long':
        di_aligned = di_plus > di_minus
        gap_abs    = di_gap
    else:
        di_aligned = di_minus > di_plus
        gap_abs    = -di_gap

    if di_aligned and gap_abs >= 15:
        di_pts = 5
    elif di_aligned and gap_abs >= 5:
        di_pts = 3
    elif abs(di_gap) < 5:
        di_pts = 1
    else:
        di_pts = 0

    # ── 6. Volume delta modifier (±3) ─────────────────────────────────────────
    if direction == 'long':
        vol_mod = 3 if vol_delta5 > 0 else (-3 if vol_delta5 < 0 else 0)
    else:
        vol_mod = 3 if vol_delta5 < 0 else (-3 if vol_delta5 > 0 else 0)

    # ── Total ─────────────────────────────────────────────────────────────────
    raw_score = adx_pts + atr_pts + ema_pts + sess_pts + di_pts + vol_mod
    score     = max(0, min(100, raw_score))

    # ── Hard overrides ────────────────────────────────────────────────────────
    hard_overrides = []
    if atr_ratio > 3.0:
        hard_overrides.append(f'ATR Ratio {atr_ratio:.1f} > 3.0 — extreme volatility')

    verdict = 'RED'
    if not hard_overrides:
        if score >= 70:
            verdict = 'GREEN'
        elif score >= 45:
            verdict = 'YELLOW'

    def _icon(pts, max_pts):
        ratio = pts / max_pts if max_pts > 0 else 0
        return '✅' if ratio >= 0.7 else ('⚠️' if ratio >= 0.35 else '❌')

    breakdown_line = (
        f"ADX: {adx_val:.1f} {_icon(adx_pts, adx_max)} ({adx_pts}/{adx_max}) | "
        f"ATR×: {atr_ratio:.2f} {_icon(atr_pts, atr_max)} ({atr_pts}/{atr_max}) | "
        f"EMA: {_icon(ema_pts, ema_max)} ({ema_pts}/{ema_max}) | "
        f"Session: ({sess_pts}/{sess_max}) | "
        f"DI: {_icon(di_pts, 5)} ({di_pts}/5) | "
        f"VolΔ: {'+' if vol_mod >= 0 else ''}{vol_mod}"
    )

    flip_condition = ''
    if verdict == 'RED' and adx_val < 20:
        flip_condition = f'ADX crosses 20 (currently {adx_val:.1f})'
    elif verdict == 'YELLOW' and adx_val < 25:
        flip_condition = f'ADX crosses 25 (currently {adx_val:.1f})'
    elif verdict == 'GREEN' and adx_declining:
        flip_condition = f'Watch: ADX declining. Below 25 → YELLOW.'

    return {
        'score':          score,
        'verdict':        verdict,
        'breakdown_line': breakdown_line,
        'flip_condition': flip_condition,
        'hard_overrides': hard_overrides,
        'adx_pts':        adx_pts,
        'atr_pts':        atr_pts,
        'ema_pts':        ema_pts,
        'sess_pts':       sess_pts,
        'di_pts':         di_pts,
        'vol_mod':        vol_mod,
    }


def _compute_enhanced_trade_plan(direction: str, close_px: float,
                                  open_px: float, high_px: float,
                                  low_px: float, atr14: float,
                                  body_pct: float) -> dict:
    """
    Multi-zone trade plan: ATR-adaptive SL, 4-zone entries (Aggressive /
    Standard 38.2% / Golden Fibo 61.8% / Sniper 78.6%), 3 TP levels per zone.
    Returns empty dict when close_px <= 0.
    """
    if close_px <= 0:
        return {}

    body_size  = abs(close_px - open_px)
    candle_rng = high_px - low_px if high_px > low_px else close_px * 0.01

    atr_buffer = atr14 if atr14 > 0 else close_px * 0.02
    atr_pct    = atr_buffer / close_px

    if direction == 'long':
        struct_sl = low_px - atr_buffer * 0.5
        struct_sl = max(struct_sl, close_px * 0.94)
        struct_sl = min(struct_sl, close_px * 0.992)
        sl_dist   = max(0.008, min(0.06, (close_px - struct_sl) / close_px))
    else:
        struct_sl = high_px + atr_buffer * 0.5
        struct_sl = min(struct_sl, close_px * 1.06)
        struct_sl = max(struct_sl, close_px * 1.008)
        sl_dist   = max(0.008, min(0.06, (struct_sl - close_px) / close_px))

    fib_382 = body_size * 0.382
    fib_618 = body_size * 0.618
    fib_786 = body_size * 0.786

    if direction == 'long':
        agg_entry      = round(close_px, 8)
        standard_entry = round(close_px - fib_382, 8)
        golden_entry   = round(close_px - fib_618, 8)
        sniper_entry   = round(close_px - fib_786, 8)
        golden_entry   = max(golden_entry, round(open_px * 1.002, 8))
        sniper_entry   = max(sniper_entry, round(open_px * 1.001, 8))
    else:
        agg_entry      = round(close_px, 8)
        standard_entry = round(close_px + fib_382, 8)
        golden_entry   = round(close_px + fib_618, 8)
        sniper_entry   = round(close_px + fib_786, 8)
        golden_entry   = min(golden_entry, round(open_px * 0.998, 8))
        sniper_entry   = min(sniper_entry, round(open_px * 0.999, 8))

    _eps = struct_sl * 0.0005
    if direction == 'short':
        std_valid    = standard_entry < struct_sl
        golden_valid = golden_entry   < struct_sl
        sniper_valid = sniper_entry   < struct_sl
        if not std_valid:
            standard_entry = round(struct_sl - _eps, 8)
        if not golden_valid:
            golden_entry   = round(struct_sl - _eps, 8)
        if not sniper_valid:
            sniper_entry   = round(struct_sl - _eps, 8)
    else:
        std_valid    = standard_entry > struct_sl
        golden_valid = golden_entry   > struct_sl
        sniper_valid = sniper_entry   > struct_sl
        if not std_valid:
            standard_entry = round(struct_sl + _eps, 8)
        if not golden_valid:
            golden_entry   = round(struct_sl + _eps, 8)
        if not sniper_valid:
            sniper_entry   = round(struct_sl + _eps, 8)

    sl_agg = sl_std = sl_golden = sl_sniper = round(struct_sl, 8)

    def _tps(entry, sl):
        risk = abs(entry - sl)
        if direction == 'long':
            return (round(entry + risk, 8), round(entry + 2 * risk, 8),
                    round(entry + 3 * risk, 8))
        else:
            return (round(entry - risk, 8), round(entry - 2 * risk, 8),
                    round(entry - 3 * risk, 8))

    tp1_a, tp2_a, tp3_a = _tps(agg_entry,      sl_agg)
    tp1_s, tp2_s, tp3_s = _tps(standard_entry, sl_std)
    tp1_g, tp2_g, tp3_g = _tps(golden_entry,   sl_golden)
    tp1_n, tp2_n, tp3_n = _tps(sniper_entry,   sl_sniper)

    return {
        'agg_entry':    agg_entry,   'agg_sl':    sl_agg,
        'agg_tp1':      tp1_a,       'agg_tp2':   tp2_a,    'agg_tp3':   tp3_a,
        'std_entry':    standard_entry, 'std_sl':  sl_std,
        'std_tp1':      tp1_s,       'std_tp2':   tp2_s,    'std_tp3':   tp3_s,
        'golden_entry': golden_entry, 'golden_sl': sl_golden,
        'golden_tp1':   tp1_g,       'golden_tp2': tp2_g,   'golden_tp3': tp3_g,
        'sniper_entry': sniper_entry, 'sniper_sl': sl_sniper,
        'sniper_tp1':   tp1_n,       'sniper_tp2': tp2_n,   'sniper_tp3': tp3_n,
        'sl_dist_pct':  round(sl_dist * 100, 2),
        'atr_pct':      round(atr_pct * 100, 2),
        'sl_method':    f'ATR-adaptive ({sl_dist*100:.1f}% — 1×ATR below/above candle structure)',
        'struct_sl':    round(struct_sl, 8),
        'std_valid':    std_valid,
        'golden_valid': golden_valid,
        'sniper_valid': sniper_valid,
    }


def _binance_klines_raw(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """
    Raw (uncached) Binance klines with backward pagination.
    Tries api.binance.com first; falls back to data-api.binance.vision.
    Returns cleaned DataFrame or empty DataFrame.
    """
    end_ms   = int(datetime.datetime.utcnow().timestamp() * 1000)
    start_ms = int((datetime.datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)

    for url, verify in _BINANCE_KLINES_URLS:
        all_klines: list = []
        batch_end = end_ms
        success   = True

        while True:
            try:
                resp = requests.get(url, params={
                    'symbol': symbol, 'interval': interval,
                    'endTime': batch_end, 'limit': 1000,
                }, timeout=10, verify=verify)
                if resp.status_code != 200:
                    success = False
                    break
                klines = resp.json()
                if not klines:
                    break
                all_klines = klines + all_klines
                earliest_ts = klines[0][0]
                if earliest_ts <= start_ms or len(klines) < 1000:
                    break
                batch_end = earliest_ts - 1
            except Exception as exc:
                log.warning('_binance_klines_raw %s %s: %s', url, symbol, exc)
                success = False
                break

        if success and all_klines:
            df = pd.DataFrame(all_klines, columns=[
                'ts', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_vol', 'n_trades',
                'taker_buy_base', 'taker_buy_quote', 'ignore',
            ])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df.set_index('ts', inplace=True)
            df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            df = df[~df.index.duplicated(keep='last')]
            df.sort_index(inplace=True)
            cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days)
            return _clean_df(df[df.index >= cutoff])

    return pd.DataFrame()


def _score_signal_bar(df: pd.DataFrame, adx_df: pd.DataFrame,
                       bar_idx: int, direction: str, timeframe: str,
                       symbol: str, min_body_pct: float = 0.65,
                       min_vol_mult: float = 1.5) -> dict | None:
    """
    Score a single bar as a momentum signal (strict scanner mode).
    Returns None if bar doesn't qualify.
    body_pct in returned dict is fraction (0-1 scale) for combo matcher compat.
    """
    try:
        bar = df.iloc[bar_idx]
    except IndexError:
        return None

    body_pct = float(bar.get('body_pct', 0) or 0)   # fraction (–1 to +1)
    vol_mult  = float(bar.get('vol_mult',  0) or 0)
    atr_ratio = float(bar.get('atr_ratio', 1) or 1)
    ema5      = float(bar.get('ema5',  0) or 0)
    ema15     = float(bar.get('ema15', 0) or 0)
    ema21     = float(bar.get('ema21', 0) or 0)
    c_rank    = float(bar.get('candle_rank_20', 0.5) or 0.5)
    v_rank    = float(bar.get('vol_rank_20',    0.5) or 0.5)
    taker_buy_ratio = float(bar.get('taker_buy_ratio', 0.5) or 0.5)
    close_px  = float(bar.get('close', 0) or 0)
    high_px   = float(bar.get('high',  close_px) or close_px)
    low_px    = float(bar.get('low',   close_px) or close_px)
    open_px   = float(bar.get('open',  close_px) or close_px)
    atr14_val = float(bar.get('atr14', close_px * 0.02) or close_px * 0.02)
    body_vs_atr_v  = float(bar.get('body_vs_atr', 0) or 0)
    dist_ema21_v   = float(bar.get('dist_from_ema21_pct', 0) or 0)

    # Direction check — strict scanner mode
    is_bullish = body_pct > 0
    if direction == 'long'  and not is_bullish:
        return None
    if direction == 'short' and is_bullish:
        return None

    # Filter thresholds
    if abs(body_pct) < min_body_pct:
        return None
    if vol_mult < min_vol_mult or pd.isna(vol_mult):
        return None
    if abs(body_pct) < 0.05:  # doji guard
        return None

    # ADX values
    adx_val  = 0.0
    di_plus  = 0.0
    di_minus = 0.0
    if adx_df is not None and not adx_df.empty and bar_idx < len(adx_df):
        try:
            _adx = float(adx_df['adx'].iloc[bar_idx])
            _dip = float(adx_df['di_plus'].iloc[bar_idx])
            _dim = float(adx_df['di_minus'].iloc[bar_idx])
            adx_val  = _adx  if _adx  == _adx  else 0.0
            di_plus  = _dip  if _dip  == _dip  else 0.0
            di_minus = _dim  if _dim  == _dim  else 0.0
        except Exception:
            pass

    # Regime score
    try:
        regime = _calculate_regime_score(
            df, bar_idx, direction, adx_df,
            timeframe=timeframe, ticker=symbol,
        )
        regime_score_val = regime.get('score',   0)
        regime_verdict   = regime.get('verdict', 'RED')
    except Exception:
        regime_score_val = 0
        regime_verdict   = 'RED'

    # Strict mode: skip RED regime
    if regime_verdict == 'RED':
        return None

    # EMA stack alignment
    if direction == 'long':
        ema_full    = (ema5 > ema15) and (ema15 > ema21)
        ema_partial = (ema5 > ema15) or  (ema15 > ema21)
    else:
        ema_full    = (ema5 < ema15) and (ema15 < ema21)
        ema_partial = (ema5 < ema15) or  (ema15 < ema21)

    # Composite score (0–100, recency added by caller)
    body_pts  = min(abs(body_pct) / 0.95, 1.0) * _SCORE_WEIGHTS['body']
    vol_norm  = max(0, (vol_mult - min_vol_mult) / max(1, 5.0 - min_vol_mult))
    vol_pts   = min(vol_norm, 1.0) * _SCORE_WEIGHTS['volume']
    adx_norm  = min(adx_val / 40.0, 1.0)
    adx_pts   = adx_norm * _SCORE_WEIGHTS['adx']
    regime_pts = (regime_score_val / 100.0) * _SCORE_WEIGHTS['regime']

    total_score = body_pts + vol_pts + adx_pts + regime_pts
    if total_score != total_score:  # NaN guard
        total_score = 0.0

    # Enhanced trade plan
    _etp = _compute_enhanced_trade_plan(
        direction=direction,
        close_px=close_px,
        open_px=open_px,
        high_px=high_px,
        low_px=low_px,
        atr14=atr14_val,
        body_pct=body_pct,
    )

    entry = _etp.get('agg_entry',  close_px)
    sl    = _etp.get('agg_sl',     close_px * (0.985 if direction == 'long' else 1.015))
    tp2r  = _etp.get('agg_tp2',    close_px)
    tp3r  = _etp.get('agg_tp3',    close_px)

    # Reasons list
    reasons = []
    bp_pct = abs(body_pct) * 100
    body_lbl = ('exceptional conviction' if bp_pct >= 85 else
                'strong conviction' if bp_pct >= 75 else 'clear momentum')
    reasons.append(f'Candle body {bp_pct:.1f}% of range — {body_lbl} '
                   f'(threshold: {min_body_pct*100:.0f}%)')

    vol_lbl = ('extreme institutional activity' if vol_mult >= 4 else
               'strong volume surge' if vol_mult >= 2.5 else
               'elevated participation' if vol_mult >= 1.8 else 'above-average volume')
    reasons.append(f'Volume {vol_mult:.1f}× the 7-bar average — {vol_lbl}')

    if adx_val >= 35:
        reasons.append(f'ADX {adx_val:.0f} — strongly trending market')
    elif adx_val >= 25:
        reasons.append(f'ADX {adx_val:.0f} — trending market')
    else:
        reasons.append(f'ADX {adx_val:.0f} — weak/moderate trend')

    di_gap_v = abs(di_plus - di_minus)
    if direction == 'long' and di_plus > di_minus and di_gap_v >= 10:
        reasons.append(f'DI+ {di_plus:.0f} vs DI− {di_minus:.0f} — bulls dominating')
    elif direction == 'short' and di_minus > di_plus and di_gap_v >= 10:
        reasons.append(f'DI− {di_minus:.0f} vs DI+ {di_plus:.0f} — bears dominating')

    if ema_full:
        reasons.append(f'EMA stack fully {"bullish (5>15>21)" if direction=="long" else "bearish (5<15<21)"}')
    elif ema_partial:
        reasons.append('EMA partially aligned')

    regime_color = {'GREEN': '✅ GREEN', 'YELLOW': '⚠️ YELLOW'}.get(regime_verdict, regime_verdict)
    reasons.append(f'Market regime {regime_color} ({regime_score_val}/100)')

    return {
        'symbol':        symbol,
        'timeframe':     timeframe,
        'direction':     direction,
        'base_score':    round(total_score, 2),
        'regime':        regime_verdict,
        'regime_score':  regime_score_val,
        # body_pct stored as fraction (0-1) for combo matcher compatibility
        'body_pct':      round(abs(body_pct), 4),
        'vol_mult':      round(vol_mult, 2),
        'adx':           round(adx_val,  1),
        'di_plus':       round(di_plus,  1),
        'di_minus':      round(di_minus, 1),
        'atr_ratio':     round(atr_ratio, 2),
        'body_vs_atr':   round(body_vs_atr_v, 2),
        'dist_from_ema21_pct': round(dist_ema21_v, 2),
        'ema_full':      ema_full,
        'ema_partial':   ema_partial,
        'candle_rank':   round(c_rank, 2),
        'vol_rank':      round(v_rank, 2),
        'close':         close_px,
        'entry':         entry,
        'sl':            sl,
        'tp2r':          tp2r,
        'tp3r':          tp3r,
        'bar_offset':    None,   # filled by caller
        'reasons':       reasons,
        '_trade_plan':   _etp,
        'taker_buy_ratio': round(taker_buy_ratio, 4),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def get_matching_combos(sig: dict, enabled_combos: list[str],
                        btc_regime: str = None,
                        allowed_levels: tuple = ('STRICT',)) -> list[dict]:
    """
    Classify sig against every combo in enabled_combos using the local level
    system.  Reads COMBOS list from quantflow_combos.py but ALL classification
    logic lives here (version-stable).

    Each returned match has _matched_level, _size_factor, _pf_haircut attached.
    Sorted by (tier, level_rank).

    Parameters
    ----------
    sig : dict
        Signal dict with keys: symbol, timeframe, direction, body_pct (fraction),
        vol_mult, adx.  body_pct must be in 0-1 scale (NOT 0-100).
    enabled_combos : list[str]
        Combo names to test against (e.g. ['C1A-A', 'C1A-N', ...]).
    btc_regime : str | None
        'BULL' | 'BEAR' | 'CHOP' | 'UNKNOWN' | None.
    allowed_levels : tuple
        Subset of ('STRICT', 'RELAXED', 'LOOSE').  Default is STRICT-only for
        backward compatibility.
    """
    if not _QFCOMBOS_OK or _qfcombos is None:
        log.error('get_matching_combos: quantflow_combos not available')
        return []

    matches = []
    for combo in _qfcombos.COMBOS:
        if combo['name'] not in enabled_combos:
            continue
        lvl = _classify_signal_level(sig, combo, btc_regime, allowed_levels)
        if lvl is None:
            continue
        mc = dict(combo)
        mc['_matched_level'] = lvl
        mc['_size_factor']   = LEVEL_SETTINGS[lvl]['size_factor']
        mc['_pf_haircut']    = LEVEL_SETTINGS[lvl]['pf_haircut']
        matches.append(mc)

    _level_rank = {'STRICT': 0, 'RELAXED': 1, 'LOOSE': 2}
    matches.sort(key=lambda c: (c['tier'],
                                _level_rank.get(c.get('_matched_level'), 9)))
    return matches


def fetch_bybit_price(symbol: str) -> dict | None:
    """
    Fetch perpetual (linear) price for a symbol from Bybit V5 public API.

    Returns dict with keys: lastPrice, markPrice, fundingRate (or None on failure).
    The Bybit symbol is the same string as Binance (BTCUSDT, ETHUSDT, etc) for
    USDT-margined perpetual contracts.

    Public endpoint, no auth needed. ~50ms per request typical latency.
    Returns None on any error so the caller can fall back to Binance price.
    """
    url = "https://api.bybit.com/v5/market/tickers"
    try:
        resp = requests.get(url, params={"category": "linear", "symbol": symbol},
                            timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("retCode") != 0:
            return None
        items = (data.get("result") or {}).get("list") or []
        if not items:
            return None
        t = items[0]
        return {
            "lastPrice":   float(t.get("lastPrice", 0)) or None,
            "markPrice":   float(t.get("markPrice", 0)) or None,
            "fundingRate": float(t.get("fundingRate", 0)) if t.get("fundingRate") else None,
            "source":      "bybit_perp",
        }
    except Exception:
        return None


def fetch_universe(min_volume_usdt: float = 500_000,
                   top_n: int = 300) -> list[str]:
    """
    Return list of USDT-quoted symbols sorted by 24h volume descending,
    filtered to >= min_volume_usdt and capped at top_n.

    Tries api.binance.com first; falls back to data-api.binance.vision mirror.
    Returns [] on complete network failure.
    """
    urls = [
        ('https://api.binance.com/api/v3/ticker/24hr',          True),
        ('https://data-api.binance.vision/api/v3/ticker/24hr',  False),
    ]
    tickers = None
    for url, verify in urls:
        try:
            resp = requests.get(url, timeout=10, verify=verify)
            resp.raise_for_status()
            tickers = resp.json()
            break
        except Exception as exc:
            log.warning('fetch_universe %s: %s', url, exc)

    if not tickers:
        return []

    universe = []
    for t in tickers:
        sym = t.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        base = sym[:-4]
        if base in _SCANNER_EXCLUDE:
            continue
        try:
            vol = float(t.get('quoteVolume', 0))
        except Exception:
            continue
        if vol < min_volume_usdt:
            continue
        universe.append({'symbol': sym, 'volume_24h': vol})

    universe.sort(key=lambda x: x['volume_24h'], reverse=True)
    return [u['symbol'] for u in universe[:top_n]]


def fetch_candles(symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
    """
    Fetch last `limit` klines for symbol/interval from Binance.
    Returns a cleaned OHLCV DataFrame (index = open_time) with all derived
    columns produced by _clean_df (body_pct, vol_mult, atr14, ema5/15/21 …).
    Returns empty DataFrame on failure.

    interval: '1h' | '4h' | '1d'  (case-insensitive)
    """
    iv = _BINANCE_INTERVAL.get(interval, interval.lower())
    urls = [
        ('https://api.binance.com/api/v3/klines',          True),
        ('https://data-api.binance.vision/api/v3/klines',  False),
    ]
    for url, verify in urls:
        try:
            resp = requests.get(
                url,
                params={'symbol': symbol, 'interval': iv, 'limit': limit},
                timeout=10,
                verify=verify,
            )
            if resp.status_code != 200:
                continue
            klines = resp.json()
            if len(klines) < 20:
                return pd.DataFrame()

            df = pd.DataFrame(klines, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'qav', 'num_trades',
                'taker_buy_base', 'tbqav', 'ignore',
            ])
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
            df.set_index('open_time', inplace=True)
            for c in ['open', 'high', 'low', 'close', 'volume', 'taker_buy_base']:
                df[c] = pd.to_numeric(df[c], errors='coerce')

            df['taker_buy_ratio'] = df.apply(
                lambda r: r['taker_buy_base'] / r['volume'] if r['volume'] > 0 else 0.5,
                axis=1,
            )
            tbr = df['taker_buy_ratio'].copy()
            df = _clean_df(df)
            if not df.empty:
                df['taker_buy_ratio'] = tbr.reindex(df.index).fillna(0.5)
            return df if not df.empty else pd.DataFrame()

        except Exception as exc:
            log.warning('fetch_candles %s %s: %s', symbol, interval, exc)
            continue

    return pd.DataFrame()


def btc_regime() -> str:
    """
    Classify the current BTC market regime from daily closes vs EMA50.

    BULL  → BTC close > EMA50 × 1.02
    BEAR  → BTC close < EMA50 × 0.98
    CHOP  → within ±2% band
    UNKNOWN → BTC data unavailable

    Uses the same definition as the QuantFlow audit (oos_audit_v3a/v3c/v3d)
    so combo *-A alignment classification stays consistent.
    """
    try:
        df = _binance_klines_raw('BTCUSDT', '1d', days=200)
        if df is None or df.empty or len(df) < 60:
            return 'UNKNOWN'
        close = df['close']
        ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
        c, e  = float(close.iloc[-1]), float(ema50.iloc[-1])
        if not (c > 0 and e > 0):
            return 'UNKNOWN'
        if c > e * 1.02:
            return 'BULL'
        if c < e * 0.98:
            return 'BEAR'
        return 'CHOP'
    except Exception as exc:
        log.warning('btc_regime: %s', exc)
        return 'UNKNOWN'


def score_signal(symbol: str, timeframe: str, direction: str,
                 bar_offset: int = 1) -> dict | None:
    """
    Fetch candles for symbol/timeframe and score the bar at bar_offset.

    bar_offset=1 → most-recent CLOSED bar (skips the live/open candle).
    bar_offset=2 → second-most-recent closed bar.  Etc.

    Returns a signal dict (same shape as app.py's _scanner_score_signal) or
    None if the bar doesn't qualify (wrong direction, below thresholds, RED
    regime, or data unavailable).

    body_pct in the returned dict is in fraction form (0-1) for direct use
    with get_matching_combos().

    Parameters
    ----------
    symbol    : e.g. 'BTCUSDT'
    timeframe : '1h' | '4h' | '1d'  (case-insensitive)
    direction : 'long' | 'short'
    bar_offset: 1 = most-recent closed candle
    """
    tf_lower = timeframe.lower()
    df = fetch_candles(symbol, tf_lower, limit=200)
    if df.empty or len(df) < 22:
        log.warning('score_signal: insufficient candles for %s %s', symbol, timeframe)
        return None

    try:
        adx_df = _calculate_adx(df)
    except Exception:
        adx_df = pd.DataFrame()

    # bar_idx: skip index –1 (live/open candle) by subtracting bar_offset+1
    bar_idx = len(df) - bar_offset - 1
    if bar_idx < 14:
        return None

    # Staleness guard: skip candles older than 5 days
    try:
        _now_utc = pd.Timestamp.utcnow().tz_localize(None)
        _bar_ts  = pd.Timestamp(df.index[bar_idx]).tz_localize(None)
        if (_now_utc - _bar_ts).total_seconds() > 5 * 86400:
            log.info('score_signal: bar too old for %s %s', symbol, timeframe)
            return None
    except Exception:
        pass

    sig = _score_signal_bar(
        df, adx_df, bar_idx, direction,
        tf_lower, symbol,
        min_body_pct=0.65, min_vol_mult=1.5,
    )
    if sig is None:
        return None

    _RECENCY_PTS = {1: 10, 2: 6, 3: 3}
    recency_pts       = _RECENCY_PTS.get(bar_offset, 0)
    sig['bar_offset'] = bar_offset
    _raw              = sig['base_score'] + recency_pts
    sig['score']      = round(_raw if _raw == _raw else 0.0, 2)

    if not sig.get('entry') or sig['entry'] != sig['entry']:
        return None

    # Timestamp in WIB (UTC+7) for display
    try:
        _ts_utc = pd.Timestamp(df.index[bar_idx])
        _ts_wib = _ts_utc + pd.Timedelta(hours=7)
        sig['candle_date'] = _ts_wib.strftime('%Y-%m-%d %H:%M WIB')
    except Exception:
        sig['candle_date'] = ''

    return sig


# ═══════════════════════════════════════════════════════════════════════════════
# SCRIPT ENTRY POINT — quick sanity check
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    print(f'Universe size: {len(fetch_universe())}')
    print(f'BTC regime: {btc_regime()}')
    sig = score_signal('BTCUSDT', '4h', 'long', bar_offset=1)
    print(f'BTCUSDT 4h long signal: {sig}')
