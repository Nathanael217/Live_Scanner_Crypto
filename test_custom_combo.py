"""Smoke test for custom combo builder."""
import sys, ast
sys.path.insert(0, "/home/claude")

with open('/home/claude/app.py') as f:
    src = f.read()

# 1. Compiles
import py_compile
py_compile.compile('/home/claude/app.py', doraise=True)
print("✓ app.py compiles")

# 2. New helper exists
assert "_qf_get_matching_combos_with_custom" in src
print("✓ Custom-combo wrapper helper present")

# 3. Custom combo synthetic-dict construction is in the code
assert '"_is_custom":  True' in src or "'_is_custom':  True" in src or '"_is_custom": True' in src
print("✓ _is_custom marker present")

# 4. The custom-combo expander exists
assert "Custom Combo Builder" in src
print("✓ Expander title present")

# 5. The dead-zone warning is present
assert "trend dead zone" in src.lower()
print("✓ Dead-zone warning present")

# 6. NO AUDIT PF text appears
assert "NO AUDIT PF" in src or "no audit PF" in src.lower()
print("✓ 'no audit PF' badge present")

# 7. Functional test: build a custom combo dict and run the classifier
# Extract the bundled level system from app.py
start = src.find("# QUANTFLOW LEVEL SYSTEM")
end = src.find("# ─── sklearn", start)
assert start > 0 and end > start
block = src[start:end]

import types, importlib
import quantflow_combos as qf
stub = types.ModuleType("stub_qf")
stub.COMBOS = qf.COMBOS
stub.COMBOS_BY_NAME = qf.COMBOS_BY_NAME
ns = {"_qfcombos": stub, "_QFCOMBOS_OK": True, "Optional": type("OS", (), {})}
exec(block, ns)

# Build a custom combo and classify a signal against it
custom = {
    "name": "CUSTOM-1", "tier": 99, "combo_type": "trend_following",
    "label_short": "CUSTOM-1", "_is_custom": True,
    "criteria": {
        "body_min": 0.50, "body_max": 0.85,
        "vol_min": 1.5, "vol_max": 5.0,
        "adx_min": 25, "adx_max": 50,
        "regime_mode": "N", "directions": ["long","short"],
    },
    "tf_eligible": ["4h"],
    "rollup": {"n": 0, "wr": 0.0, "mean_r": 0.0, "sharpe": 0.0, "pf": 0.0},
    "primary": {"tf": "4h", "direction": "long", "entry_zone": "0%", "tp_R": 2.0,
                "sizing": "SMALL", "n":0, "wr":0.0, "mean_r":0.0, "pf":0.0},
}

# Body 0.65 (dead zone) — must still reject
sig = {"symbol":"T", "timeframe":"4h", "direction":"long",
       "body_pct": 0.65, "vol_mult": 2.0, "adx": 30}
lvl = ns["_qf_classify_signal_level"](sig, custom, btc_regime="BULL",
                                        allowed_levels=("STRICT","RELAXED","LOOSE"))
assert lvl is None, f"Custom combo MUST respect dead zone, but got {lvl}"
print("✓ Custom combo respects body 0.60-0.70 dead zone")

# Body 0.75 (in user's 0.50-0.85 range, outside dead zone) — must match
sig["body_pct"] = 0.75
lvl = ns["_qf_classify_signal_level"](sig, custom, btc_regime="BULL",
                                        allowed_levels=("STRICT",))
assert lvl == "STRICT", f"Body 0.75 should match user's 0.50-0.85 range, got {lvl}"
print("✓ Body 0.75 matches user-defined band at STRICT")

# ADX 51 — must still reject
sig["body_pct"] = 0.75
sig["adx"] = 51
lvl = ns["_qf_classify_signal_level"](sig, custom, btc_regime="BULL",
                                        allowed_levels=("STRICT","RELAXED","LOOSE"))
assert lvl is None, f"Custom combo MUST respect ADX 50 cap, got {lvl}"
print("✓ Custom combo respects ADX 50 hard cap")

print("\nAll custom-combo tests pass")
