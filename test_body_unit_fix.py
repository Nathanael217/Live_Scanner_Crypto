import sys
sys.path.insert(0, "/home/claude")

with open("/home/claude/app.py") as f:
    src = f.read()
start = src.find("# QUANTFLOW LEVEL SYSTEM")
end = src.find("# ─── sklearn", start)
block = src[start:end]

import types, importlib.util
import quantflow_combos as qf
stub = types.ModuleType("stub")
stub.COMBOS = qf.COMBOS
stub.COMBOS_BY_NAME = qf.COMBOS_BY_NAME
ns = {"_qfcombos": stub, "_QFCOMBOS_OK": True, "Optional": type("OS", (), {})}
exec(block, ns)

# Same signal, two formats
sig_app    = {"symbol":"BTCUSDT","timeframe":"4h","direction":"long",
              "body_pct": 75.0,  "vol_mult": 2.5, "adx": 35.0}   # app format
sig_worker = {"symbol":"BTCUSDT","timeframe":"4h","direction":"long",
              "body_pct": 0.75,  "vol_mult": 2.5, "adx": 35.0}   # worker format

names = [c["name"] for c in qf.COMBOS]
m_app    = ns["_qf_get_matching_combos"](sig_app,    names, btc_regime="BULL",
                                            allowed_levels=("STRICT","RELAXED"))
m_worker = ns["_qf_get_matching_combos"](sig_worker, names, btc_regime="BULL",
                                            allowed_levels=("STRICT","RELAXED"))
assert len(m_app) == len(m_worker), \
    f"Unit fix failed: app {len(m_app)} vs worker {len(m_worker)}"
print(f"✓ Unit fix works: {len(m_app)} matches in both formats")

# Hard caps still enforced — body=65 (percent) = 0.65 fraction = dead zone
sig_dead = {"symbol":"T","timeframe":"4h","direction":"long",
            "body_pct": 65.0, "vol_mult": 2.0, "adx": 30.0}
m = ns["_qf_get_matching_combos"](sig_dead, names, btc_regime="BULL",
                                    allowed_levels=("STRICT","RELAXED","LOOSE"))
assert len(m) == 0, f"Dead zone leak: body=65% (0.65) matched {len(m)}"
print("✓ Dead zone hard cap still enforced after fix")

# Body=0.65 (already fraction) also rejected
sig_dead2 = dict(sig_dead); sig_dead2["body_pct"] = 0.65
m = ns["_qf_get_matching_combos"](sig_dead2, names, btc_regime="BULL",
                                    allowed_levels=("STRICT","RELAXED","LOOSE"))
assert len(m) == 0
print("✓ Dead zone enforced for both unit conventions")

# ADX 51 still rejects
sig_high = {"symbol":"T","timeframe":"4h","direction":"long",
            "body_pct": 75.0, "vol_mult": 2.0, "adx": 51.0}
m = ns["_qf_get_matching_combos"](sig_high, names, btc_regime="BULL",
                                    allowed_levels=("STRICT","RELAXED","LOOSE"))
assert len(m) == 0
print("✓ ADX 50 cap still enforced")

print("\nAll body_pct unit tests pass")
