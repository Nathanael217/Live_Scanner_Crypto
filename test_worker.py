import sys
sys.path.insert(0, "/home/claude")
from scanner_worker import format as fmt

# === Bybit price + enriched format tests ===
print("\n[NEW] Bybit price + enriched format")

sig_with_extras = {
    "symbol":           "BTCUSDT",
    "timeframe":        "4h",
    "direction":        "short",
    "body_pct":         0.81,
    "vol_mult":         2.4,
    "adx":              38,
    "candle_close_iso": "2026-05-04T16:00:00Z",
    "_btc_regime_at_scan": "BULL",
    "_bybit_price": {
        "lastPrice": 58432.5, "markPrice": 58430.0,
        "fundingRate": 0.0001, "source": "bybit_perp",
    },
}
match = {
    "name": "C6A-N", "tier": 1, "combo_type": "trend_following",
    "_matched_level": "RELAXED", "_size_factor": 0.75, "_pf_haircut": 0.92,
    "rollup": {"pf": 1.42},
    "criteria": {"regime_mode": "N"},
    "primary": {"tf": "4h", "direction": "short", "entry_zone": "0%",
                "tp_R": 2.0, "sizing": "FULL"},
}
text = fmt.format_signal(sig_with_extras, match)
print(f"  Enriched message ({len(text)} chars):")
for line in text.split("\n"):
    print(f"    | {line}")

assert "Tier 1" in text, "should show Tier 1 group, not Tier 1 rank"
assert "Bybit perp" in text and "58432.5" in text
assert "Plan:" in text
assert "Macro:" in text
assert "BULL" in text
assert "effective risk" in text.lower()
assert len(text) < 800, f"message too long: {len(text)}"
print("  ✓ Enriched message has tier group, Bybit price, plan, macro, eff risk")

# CT version
match["combo_type"] = "countertrend"
match["primary"] = {"tf": "4h", "direction": "long", "entry_retrace": -0.30,
                    "sl_method": "wick_anchor", "tp_R": 2.0, "sizing": "HALF"}
text_ct = fmt.format_signal(sig_with_extras, match)
assert "FADE PLAN" in text_ct
assert "Tier 3" in text_ct
print("  ✓ CT format shows Tier 3 + FADE PLAN")

# No Bybit price (fallback)
sig_with_extras["_bybit_price"] = None
text_no = fmt.format_signal(sig_with_extras, match)
assert "N/A" in text_no
print("  ✓ Bybit-N/A fallback works")

# tier_group_label edge cases
from scanner_worker.format import tier_group_label
assert tier_group_label({"tier": 4, "combo_type": "trend_following"}) == "Tier 1"
assert tier_group_label({"tier": 6, "combo_type": "trend_following"}) == "Tier 2"
assert tier_group_label({"tier": 14, "combo_type": "countertrend"}) == "Tier 3"
print("  ✓ tier_group_label resolves all cases")

print("\nAll Fix-4 tests pass")
