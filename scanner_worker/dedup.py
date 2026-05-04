"""
Persist 'already alerted' signals across runs via state.json.

State file is committed back to the repo by the GitHub Actions workflow,
so dedup persists across runs even though the runner is ephemeral.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
TTL_DAYS   = 7   # purge entries older than this


def _load_state() -> dict:
    """Returns {} if file missing or unreadable."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomic-ish write."""
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def _purge_old(state: dict) -> dict:
    """Drop entries whose sent_at is older than TTL_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
    out = {}
    for key, v in state.items():
        try:
            sent_at = datetime.fromisoformat(v["sent_at"])
            if sent_at >= cutoff:
                out[key] = v
        except Exception:
            pass   # bad entries get dropped
    return out


def signal_key(sig: dict, combo_name: str, level: str) -> str:
    """Stable key. Candle-close-ts not bar_offset (which changes between scans)."""
    candle_ts = sig.get("candle_close_iso") or sig.get("ts") or ""
    return (
        f"{sig['symbol']}_{sig['timeframe']}_{sig['direction']}_"
        f"{candle_ts}_{combo_name}_{level}"
    )


def already_sent(sig: dict, combo_name: str, level: str) -> bool:
    """Check the on-disk state."""
    state = _load_state()
    return signal_key(sig, combo_name, level) in state


def mark_sent(sig: dict, combo_name: str, level: str) -> None:
    """Append to state and persist (with auto-purge)."""
    state = _purge_old(_load_state())
    state[signal_key(sig, combo_name, level)] = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "symbol":  sig["symbol"],
        "tf":      sig["timeframe"],
        "dir":     sig["direction"],
        "combo":   combo_name,
        "level":   level,
    }
    _save_state(state)
