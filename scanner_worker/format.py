"""Format a (sig, combo_match) into a Telegram-friendly message.

Telegram supports up to 4096 chars but we want notification-friendly (<500).
Use plain text — markdown V2 is annoying to escape correctly.
"""


def _level_emoji(level: str) -> str:
    return {"STRICT": "🟢", "RELAXED": "🟡", "LOOSE": "🟠"}.get(level, "⚪")


def _direction_emoji(direction: str) -> str:
    return "🔺" if direction == "long" else "🔻"


def format_signal(sig: dict, primary_match: dict) -> str:
    """
    Build a notification body from a sig + its primary combo match.

    Required sig keys: symbol, timeframe, direction, body_pct, vol_mult, adx.
    Required match keys: name, tier, _matched_level, _size_factor, _pf_haircut,
                         rollup (with .pf), combo_type (optional).
    """
    sym   = sig["symbol"]
    tf    = sig["timeframe"].upper()
    dir_  = sig["direction"].upper()
    body  = abs(float(sig.get("body_pct", 0)))
    vol   = float(sig.get("vol_mult", 0))
    adx   = float(sig.get("adx", 0))

    name  = primary_match["name"]
    tier  = primary_match.get("tier", "?")
    level = primary_match["_matched_level"]
    sf    = float(primary_match["_size_factor"])
    h     = float(primary_match["_pf_haircut"])
    audit_pf    = float(primary_match["rollup"]["pf"])
    expected_pf = audit_pf * h
    is_ct       = primary_match.get("combo_type") == "countertrend"

    lines = []
    lines.append(f"{_direction_emoji(dir_)} {sym} {tf} {dir_}")
    lines.append(f"{_level_emoji(level)} Level: {level} · Combo: {name} (Tier {tier})")
    if is_ct:
        lines.append(
            "⚠ COUNTERTREND — fade the candle (trade direction is OPPOSITE)"
        )
    lines.append("")
    lines.append(f"Candle: body {body:.2f}, vol {vol:.1f}x, ADX {adx:.0f}")
    if level == "STRICT":
        lines.append(f"Audit PF: {audit_pf:.2f} (full sizing)")
    else:
        lines.append(
            f"Audit PF: {audit_pf:.2f} → expected ~{expected_pf:.2f} (×{h:.2f})"
        )
        lines.append(f"Sizing: {sf:.2f}× of stated")
    lines.append("")
    lines.append("Open the app for full details, AI verdict, decision matrix.")

    return "\n".join(lines)


def format_summary(n_total: int, n_sent: int) -> str:
    """End-of-run summary if we want a single status message instead of per-signal."""
    return f"📡 Scan done: {n_total} signal(s) found, {n_sent} new alert(s) sent."
