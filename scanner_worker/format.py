"""Format a (sig, combo_match) into a Telegram-friendly message.

Telegram supports up to 4096 chars but we want notification-friendly (<500).
Use plain text — markdown V2 is annoying to escape correctly.
"""


def tier_group_label(combo: dict) -> str:
    """
    Convert raw audit rank (combo['tier'], 1-17) to user-facing tier group.

    Mirrors the grouping used in app.py's combo expander UI:
      - countertrend (combo_type='countertrend')  -> "Tier 3"
      - trend rank 1-5                            -> "Tier 1"
      - trend rank 6-10                           -> "Tier 2"
      - anything else                             -> "Tier ?"

    This way the Telegram message uses the same tier vocabulary the user
    sees in the app, instead of exposing the raw audit rank position.
    """
    if combo.get("combo_type") == "countertrend":
        return "Tier 3"
    rank = int(combo.get("tier", 99))
    if 1 <= rank <= 5:
        return "Tier 1"
    if 6 <= rank <= 10:
        return "Tier 2"
    return "Tier ?"


def _level_emoji(level: str) -> str:
    return {"STRICT": "🟢", "RELAXED": "🟡", "LOOSE": "🟠"}.get(level, "⚪")


def _direction_emoji(direction: str) -> str:
    return "🔺" if direction == "long" else "🔻"


def _effective_risk_pct(sizing: str, size_factor: float) -> float:
    """Effective % risk after applying level size_factor to base sizing."""
    base = {"LARGE": 0.75, "FULL": 0.50, "HALF": 0.25, "SMALL": 0.15}.get(sizing, 0.50)
    return base * size_factor


def _regime_alignment(direction: str, regime: str, regime_mode: str) -> str:
    """One-line alignment indicator for the macro section."""
    if regime_mode != "A":
        return f"{regime} (regime not required)"
    if regime == "BULL" and direction == "long":
        return f"{regime} · aligned ✓"
    if regime == "BEAR" and direction == "short":
        return f"{regime} · aligned ✓"
    if regime in ("CHOP", "UNKNOWN"):
        return f"{regime} · ambiguous"
    return f"{regime} · FIGHTING ⚠"


def format_signal(sig: dict, primary_match: dict) -> str:
    """
    Build a notification body from a sig + its primary combo match.

    Required sig keys: symbol, timeframe, direction, body_pct, vol_mult, adx.
    Optional sig keys: _bybit_price (dict or None), _btc_regime_at_scan (str).
    Required match keys: name, tier, _matched_level, _size_factor, _pf_haircut,
                         rollup (.pf), criteria (.regime_mode), primary
                         (.tf, .direction, optional .entry_zone, .entry_retrace,
                          .sl_method, .tp_R, .sizing).
    """
    sym   = sig["symbol"]
    tf    = sig["timeframe"].upper()
    dir_  = sig["direction"].upper()
    body  = abs(float(sig.get("body_pct", 0)))
    # Auto-normalize body_pct from percent (0-100) to fraction (0-1) for display.
    # The app stores 75.0; the worker stores 0.75. Either way show as 0.75.
    if body > 1.5:
        body = body / 100.0
    vol   = float(sig.get("vol_mult", 0))
    adx   = float(sig.get("adx", 0))

    name  = primary_match["name"]
    level = primary_match["_matched_level"]
    sf    = float(primary_match["_size_factor"])
    h     = float(primary_match["_pf_haircut"])
    audit_pf    = float(primary_match["rollup"]["pf"])
    expected_pf = audit_pf * h
    sizing      = primary_match.get("primary", {}).get("sizing", "FULL")
    eff_pct     = _effective_risk_pct(sizing, sf)
    is_ct       = primary_match.get("combo_type") == "countertrend"
    plan        = primary_match.get("primary") or {}
    crit        = primary_match.get("criteria") or {}

    btc_regime  = sig.get("_btc_regime_at_scan", "UNKNOWN")
    bybit       = sig.get("_bybit_price")     # dict or None

    lines = []
    lines.append(f"{_direction_emoji(dir_)} {sym} {tf} {dir_}")
    lines.append(f"{_level_emoji(level)} Level: {level} · Combo: {name} "
                 f"({tier_group_label(primary_match)})")
    if is_ct:
        lines.append(
            "⚠ COUNTERTREND — fade the candle (trade direction is OPPOSITE)"
        )
    lines.append("")

    # Candle stats
    lines.append(f"📊 Candle: body {body:.2f}, vol {vol:.1f}×, ADX {adx:.0f}")
    if level == "STRICT":
        lines.append(f"   Audit PF: {audit_pf:.2f} (full sizing)")
    else:
        lines.append(f"   Audit PF: {audit_pf:.2f} → expected ~{expected_pf:.2f} "
                     f"(×{h:.2f})")
        lines.append(f"   Sizing: {sf:.2f}× of stated  "
                     f"(effective risk: {eff_pct:.2f}%)")

    # Bybit perp price — note the source clearly
    if bybit and bybit.get("lastPrice"):
        funding_str = ""
        if bybit.get("fundingRate") is not None:
            funding_str = f", funding {bybit['fundingRate']*100:+.4f}%"
        lines.append(f"💱 Bybit perp: {bybit['lastPrice']:.6g}{funding_str}")
    else:
        lines.append(f"💱 Bybit perp: N/A (use Binance spot/perp for entry)")

    # Trade plan
    plan_tf  = (plan.get("tf") or sig.get("timeframe") or "?").upper()
    plan_dir = (plan.get("direction") or sig.get("direction") or "?").upper()
    if is_ct:
        retrace = plan.get("entry_retrace", 0)
        sl_m    = plan.get("sl_method", "?")
        tp_R    = plan.get("tp_R", "?")
        lines.append(f"🎯 FADE PLAN: {plan_tf} {plan_dir} (opposite of candle), "
                     f"retrace {retrace:+.2f}, SL {sl_m}, TP {tp_R}R")
    else:
        entry = plan.get("entry_zone", "0%")
        tp_R  = plan.get("tp_R", "?")
        lines.append(f"🎯 Plan: {plan_tf} {plan_dir}, entry {entry}, TP {tp_R}R")

    # Macro alignment
    regime_mode = crit.get("regime_mode", "N")
    lines.append(f"🌐 Macro: {_regime_alignment(dir_.lower(), btc_regime, regime_mode)}")

    lines.append("")
    lines.append("🔗 Open the app for AI verdict + ML score + decision matrix")

    return "\n".join(lines)


def format_summary(n_total: int, n_sent: int) -> str:
    """End-of-run summary if we want a single status message instead of per-signal."""
    return f"📡 Scan done: {n_total} signal(s) found, {n_sent} new alert(s) sent."
