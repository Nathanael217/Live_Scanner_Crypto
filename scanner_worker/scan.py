"""
Main entry point. Run with: python -m scanner_worker.scan

Scans the universe, finds combo matches at all 3 confidence levels, dedups,
and sends Telegram alerts for new matches.

Env vars:
  TG_BOT_TOKEN, TG_CHAT_ID  — required
  QF_TIMEFRAMES             — optional, default "4h,1d"
  QF_TOP_N                  — optional, default 300
  QF_MIN_VOLUME_USDT        — optional, default 500000
  QF_DRY_RUN                — optional, "1" to skip Telegram send (test mode)
"""
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Make the parent dir importable so quantflow_scanner_core resolves
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quantflow_combos as qf
import quantflow_scanner_core as qsc
from scanner_worker import dedup
from scanner_worker import format as fmt
from scanner_worker import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("qf.scan")


def get_config() -> dict:
    return {
        "timeframes": os.environ.get("QF_TIMEFRAMES", "4h,1d").split(","),
        "top_n":      int(os.environ.get("QF_TOP_N", "300")),
        "min_volume": float(os.environ.get("QF_MIN_VOLUME_USDT", "500000")),
        "dry_run":    os.environ.get("QF_DRY_RUN", "0") == "1",
    }


def scan_one(
    symbol: str,
    tf: str,
    direction: str,
    btc_regime: str,
    all_combo_names: list,
) -> list:
    """Score one (symbol, tf, direction) and return matches at all 3 levels.
    Returns [] on any error so a bad coin doesn't kill the loop."""
    try:
        sig = qsc.score_signal(symbol, tf, direction, bar_offset=1)
        if sig is None:
            return []
        # Attach BTC regime for the formatter's macro alignment line
        sig["_btc_regime_at_scan"] = btc_regime
        # Fetch Bybit perp price. Tolerated to fail — formatter falls back to "N/A".
        sig["_bybit_price"] = qsc.fetch_bybit_price(symbol)
        matches = qsc.get_matching_combos(
            sig,
            all_combo_names,
            btc_regime=btc_regime,
            allowed_levels=("STRICT", "RELAXED", "LOOSE"),
        )
        return [(sig, m) for m in matches] if matches else []
    except Exception as e:
        log.warning(f"Scan failed for {symbol} {tf} {direction}: {e}")
        return []


def main() -> int:
    cfg = get_config()
    log.info(
        f"Starting scan: tfs={cfg['timeframes']}, top_n={cfg['top_n']}, "
        f"dry_run={cfg['dry_run']}"
    )
    t0 = time.time()

    # Validate Telegram creds early (unless dry run)
    if not cfg["dry_run"] and not notify.test_credentials():
        log.error("Telegram credentials invalid or missing. Aborting.")
        return 1

    universe = qsc.fetch_universe(
        min_volume_usdt=cfg["min_volume"],
        top_n=cfg["top_n"],
    )
    if not universe:
        log.error("Empty universe — fetch_universe failed")
        return 1
    log.info(f"Universe: {len(universe)} symbols")

    btc_regime = qsc.btc_regime()
    log.info(f"BTC regime: {btc_regime}")

    all_combo_names = [c["name"] for c in qf.COMBOS]

    n_total_matches = 0
    n_sent          = 0
    n_dedup         = 0
    n_failed_send   = 0

    # Build the work queue: every (symbol, tf, direction) triple we need to scan
    work_items = [
        (symbol, tf, direction)
        for symbol in universe
        for tf in cfg["timeframes"]
        for direction in ("long", "short")
    ]
    log.info(f"Scanning {len(work_items)} (symbol, tf, dir) triples in parallel...")

    # Parallelize with a thread pool. Binance allows ~1200 weight/min on the
    # public klines endpoint, and our limit=200 calls cost ~1 weight each.
    # 16 workers × ~0.5s per request = ~32 calls/sec, safely under the limit.
    # If Binance rate-limits, scan_one's try/except absorbs the error and
    # logs a warning, so the run continues.
    n_workers = int(os.environ.get("QF_WORKERS", "16"))

    all_hits = []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(scan_one, sym, tf, d, btc_regime, all_combo_names):
                (sym, tf, d)
            for (sym, tf, d) in work_items
        }
        for fut in as_completed(futures):
            try:
                hits = fut.result()
                all_hits.extend(hits)
            except Exception as e:
                triple = futures[fut]
                log.warning(f"Worker thread crashed for {triple}: {e}")

    log.info(f"Parallel scan complete: {len(all_hits)} (sig, match) pairs found")

    # Now process matches sequentially — Telegram sends must be serialized
    # to respect rate limits, and dedup state.json updates must be atomic.
    for sig, match in all_hits:
        n_total_matches += 1
        name  = match["name"]
        level = match["_matched_level"]

        if dedup.already_sent(sig, name, level):
            n_dedup += 1
            continue

        if cfg["dry_run"]:
            log.info(f"[DRY] would send: {sig['symbol']} {sig['timeframe']} "
                     f"{sig['direction']} {name} {level}")
            n_sent += 1
            continue

        text = fmt.format_signal(sig, match)
        if notify.send_message(text):
            dedup.mark_sent(sig, name, level)
            n_sent += 1
            log.info(f"Sent: {sig['symbol']} {sig['timeframe']} {sig['direction']} "
                     f"{name} {level}")
        else:
            n_failed_send += 1
            log.error(f"Send failed: {sig['symbol']} {sig['timeframe']} "
                      f"{sig['direction']} {name}")

    elapsed = time.time() - t0
    log.info(
        f"Done in {elapsed:.1f}s: {n_total_matches} matches, "
        f"{n_sent} sent, {n_dedup} deduped, {n_failed_send} failed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
