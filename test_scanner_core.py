"""Sanity test for quantflow_scanner_core."""
import sys
sys.path.insert(0, "/home/claude")

import quantflow_scanner_core as qsc
import quantflow_combos as qf

# 1. Import works without Streamlit
import importlib
spec = importlib.util.find_spec("streamlit")
# Don't import streamlit; just verify qsc doesn't either
import quantflow_scanner_core
assert "streamlit" not in str(quantflow_scanner_core.__dict__).lower(), "qsc imports streamlit!"
print("✓ No Streamlit dependency")

# 2. Constants present
assert qsc.LEVELS == ('STRICT', 'RELAXED', 'LOOSE')
assert qsc.LEVEL_SETTINGS['RELAXED']['size_factor'] == 0.75
print("✓ Constants OK")

# 3. _widen_criteria asymmetry — body 0.6-0.7 dead zone protected
c1a = qf.COMBOS_BY_NAME['C1A-A']  # body 0.50-0.60 trend
loose = qsc._widen_criteria(c1a, 'LOOSE')
assert loose['body_max'] == 0.60, f"DEAD ZONE LEAK: {loose['body_max']}"
c6a = qf.COMBOS_BY_NAME['C6A-A']  # body 0.70-0.80 trend
loose = qsc._widen_criteria(c6a, 'LOOSE')
assert loose['body_min'] == 0.70, f"DEAD ZONE LEAK: {loose['body_min']}"
print("✓ Body dead zone hard cap enforced at all levels")

# 4. ADX 50 cap
c5b = qf.COMBOS_BY_NAME['C5B-A']  # adx 40-50
loose = qsc._widen_criteria(c5b, 'LOOSE')
assert loose['adx_max'] == 50.0
print("✓ ADX 50 hard cap enforced")

# 5. CT body 0.78 floor
sig = {'symbol':'TEST','timeframe':'4h','direction':'short',
       'body_pct': 0.77, 'vol_mult': 9.0, 'adx': 35.0}
ct_names = [c['name'] for c in qf.COMBOS if c.get('combo_type')=='countertrend']
matches = qsc.get_matching_combos(sig, ct_names, btc_regime='BULL',
                                   allowed_levels=('STRICT','RELAXED','LOOSE'))
assert len(matches) == 0, f"CT FLOOR LEAK: body 0.77 matched {len(matches)}"
print("✓ CT body 0.78 floor enforced")

# 6. Default = STRICT only (backward compat)
sig['body_pct'] = 0.81
matches_default = qsc.get_matching_combos(sig, [c['name'] for c in qf.COMBOS],
                                            btc_regime='BULL')
matches_strict = qsc.get_matching_combos(sig, [c['name'] for c in qf.COMBOS],
                                           btc_regime='BULL',
                                           allowed_levels=('STRICT',))
assert len(matches_default) == len(matches_strict)
print("✓ Default arg = STRICT only (backward compat)")

# 7. Live HTTP smoke test (skipped if network unavailable)
try:
    universe = qsc.fetch_universe(top_n=10)
    print(f"✓ fetch_universe returned {len(universe)} symbols")
    if len(universe) > 0:
        df = qsc.fetch_candles(universe[0], '4h', limit=20)
        print(f"✓ fetch_candles returned {len(df)} bars for {universe[0]}")
except Exception as e:
    print(f"⚠ Live HTTP unavailable (sandbox firewall expected): {e}")

print("\nAll core logic tests pass")
