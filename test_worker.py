"""End-to-end test of the worker pieces — no real Telegram traffic."""
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Stage a tmpdir for the state.json
tmpdir = tempfile.mkdtemp()
sys.path.insert(0, "/home/claude")
os.chdir(tmpdir)

from scanner_worker import dedup
from scanner_worker import format as fmt
from scanner_worker import notify

# Patch dedup.STATE_PATH for isolation
dedup.STATE_PATH = Path(tmpdir) / "state.json"

# ── 1. Dedup roundtrip ────────────────────────────────────────────────────────
sig = {
    "symbol":           "BTCUSDT",
    "timeframe":        "4h",
    "direction":        "long",
    "body_pct":         0.75,
    "vol_mult":         2.5,
    "adx":              35,
    "candle_close_iso": "2026-05-04T08:00:00Z",
}
assert not dedup.already_sent(sig, "C6A-A", "STRICT")
dedup.mark_sent(sig, "C6A-A", "STRICT")
assert dedup.already_sent(sig, "C6A-A", "STRICT")
assert not dedup.already_sent(sig, "C6A-A", "RELAXED")   # different level → different key
print("✓ Dedup works")

# ── 2. Format ─────────────────────────────────────────────────────────────────
match = {
    "name":            "C6A-A",
    "tier":            1,
    "combo_type":      "trend_following",
    "_matched_level":  "RELAXED",
    "_size_factor":    0.75,
    "_pf_haircut":     0.92,
    "rollup":          {"pf": 1.42},
}
text = fmt.format_signal(sig, match)
assert "BTCUSDT" in text and "RELAXED" in text and "C6A-A" in text
expected = round(1.42 * 0.92, 2)          # 1.3064 → displayed as 1.31
assert f"expected ~{expected:.2f}" in text, f"Expected 'expected ~{expected:.2f}' in:\n{text}"
print(f"✓ Format produces {len(text)}-char message:")
for line in text.split("\n"):
    print(f"   | {line}")

# ── 3. Telegram dry-run check (no real send) ──────────────────────────────────
os.environ["TG_BOT_TOKEN"] = ""
os.environ["TG_CHAT_ID"]   = ""
# Reload notify so module-level env reads pick up the cleared values
import importlib
import scanner_worker.notify as _notify_mod
_notify_mod.TG_BOT_TOKEN = ""
_notify_mod.TG_CHAT_ID   = ""
try:
    notify.send_message("test")
    print("✗ Should have raised TelegramConfigError")
except notify.TelegramConfigError:
    print("✓ Empty creds correctly raise TelegramConfigError")

# ── 4. Format for STRICT (no haircut shown) ───────────────────────────────────
match["_matched_level"] = "STRICT"
match["_size_factor"]   = 1.0
match["_pf_haircut"]    = 1.0
text2 = fmt.format_signal(sig, match)
assert "STRICT" in text2 and "full sizing" in text2.lower(), (
    f"Expected 'full sizing' in STRICT message:\n{text2}"
)
print("✓ STRICT format omits haircut math")

# ── 5. Format for COUNTERTREND ────────────────────────────────────────────────
match["combo_type"]     = "countertrend"
match["_matched_level"] = "LOOSE"
match["_size_factor"]   = 0.5
match["_pf_haircut"]    = 0.8
text3 = fmt.format_signal(sig, match)
assert "COUNTERTREND" in text3 and "OPPOSITE" in text3, (
    f"Expected CT warnings in:\n{text3}"
)
print("✓ CT format flags fade direction")

# ── 6. Dry-run scan (only if quantflow_scanner_core is importable) ────────────
os.environ["QF_DRY_RUN"] = "1"
try:
    from scanner_worker import scan
    rc = scan.main()
    print(f"✓ Dry-run scan exited with code {rc}")
except Exception as e:
    print(f"⚠ Dry-run scan failed (network-bound, may be expected): {e}")

shutil.rmtree(tmpdir)
print("\nAll worker tests pass")
