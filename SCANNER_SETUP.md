# QuantFlow Scanner — Telegram Live Alerts

The scanner runs on GitHub Actions every 4 hours and sends Telegram messages
for any signal that matches a backtest-validated combo at any confidence
level (STRICT, RELAXED, or LOOSE). Each alert is labeled with the level so
you can size accordingly.

## One-time setup (5 minutes)

### 1. Create a Telegram bot

1. Open Telegram, message `@BotFather`.
2. Send `/newbot`, choose a name and username.
3. Save the **bot token** — looks like `1234567890:ABCdefGhi...`.
4. Send any message to your new bot (this initialises the chat).

### 2. Find your chat ID

Open this URL in a browser, replacing `<TOKEN>`:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Find `"chat":{"id":<NUMBER>` in the JSON. That `<NUMBER>` is your chat ID.
If the result is empty, make sure you sent the bot a message first (step 1.4).

### 3. Add secrets to GitHub

In your repo: **Settings → Secrets and variables → Actions → New secret**.

Add two secrets:

| Secret name    | Value                          |
|----------------|--------------------------------|
| `TG_BOT_TOKEN` | Bot token from step 1          |
| `TG_CHAT_ID`   | Chat ID number from step 2     |

Secret names are case-sensitive — use exactly the names above.

### 4. Enable the workflow

Push these files to your default branch:

```
.github/workflows/scanner.yml
scanner_worker/          (full package)
quantflow_scanner_core.py
quantflow_combos.py
requirements.txt
```

The workflow runs automatically on the cron schedule. To trigger immediately:

**Repo → Actions tab → "QuantFlow Scanner Cron" → "Run workflow" button.**

---

## Configuration

All tunable settings live in the `env:` block of `.github/workflows/scanner.yml`.
Edit and push to change — no redeploy needed.

| Variable              | Default   | Purpose                                      |
|-----------------------|-----------|----------------------------------------------|
| `QF_TIMEFRAMES`       | `4h,1d`   | Comma-separated timeframes to scan           |
| `QF_TOP_N`            | `300`     | Top N coins by 24h USDT volume               |
| `QF_MIN_VOLUME_USDT`  | `500000`  | Skip coins below this daily volume           |
| `QF_DRY_RUN`          | `0`       | Set to `1` to log only, no Telegram sends    |

---

## What the alerts look like

Every signal matching a combo sends one Telegram message per combo match:

```
🔺 BTCUSDT 4H LONG
🟢 Level: STRICT · Combo: C6A-A (Tier 1)

Candle: body 0.74, vol 2.8x, ADX 36
Audit PF: 1.42 (full sizing)

Open the app for full details, AI verdict, decision matrix.
```

**Level legend:**

- 🟢 **STRICT** — exact audit-validated criteria, full sizing
- 🟡 **RELAXED** — slightly widened bands, 0.75× sizing, ~92% of audit PF expected
- 🟠 **LOOSE** — wider bands, 0.50× sizing, ~80% of audit PF expected

For countertrend (CT) combos, the message includes:

```
⚠ COUNTERTREND — fade the candle (trade direction is OPPOSITE)
```

---

## Dedup logic

The same signal won't alert twice. The dedup key is:

```
{symbol}_{timeframe}_{direction}_{candle_close_ts}_{combo}_{level}
```

Intentional behaviour:

- One signal matching **multiple combos** → one message per combo (each has different audit stats)
- One signal matching at **multiple levels** → one message per level (STRICT and LOOSE are materially different sizing decisions)
- Same combo + level on the **same candle** → never alerts twice, even across runs

Dedup state is stored in `scanner_worker/state.json` and committed back to
the repo after every run. Entries older than 7 days are purged automatically.

---

## GitHub Actions cost

| Metric              | Value                              |
|---------------------|------------------------------------|
| Runs per day        | 6 (every 4 hours)                  |
| Avg run time        | ~3 minutes                         |
| Monthly usage       | ~540 minutes                       |
| Free tier (private) | 2,000 minutes/month                |
| Free tier (public)  | Unlimited                          |

Well within free tier for both public and private repos.

---

## Local testing (no Telegram messages sent)

```bash
QF_DRY_RUN=1 python -m scanner_worker.scan
```

This runs the full scan pipeline and logs what it would have sent, without
making any Telegram API calls. Safe to run as many times as you like.

---

## Troubleshooting

**Scan ran but no alerts received.**
Check the GitHub Actions log — it shows every combo match and whether it was
sent or deduped. If nothing matched, no coins passed the criteria that run.

**"TelegramConfigError: TG_BOT_TOKEN and TG_CHAT_ID must be set".**
The secrets are missing or misspelled. Go to repo Settings → Secrets →
Actions and verify both names are exact (case-sensitive).

**Bot token works locally but fails on GitHub Actions.**
The token may have a space or newline — copy it carefully from BotFather,
no surrounding whitespace.

**Workflow fails to push `state.json`.**
The repo's default workflow permissions may be read-only.
Fix: **Repo Settings → Actions → General → Workflow permissions →
"Read and write permissions".**

**`getUpdates` returns empty.**
You haven't sent the bot a message yet. Open the bot chat in Telegram,
send any text (e.g. `/start`), then call `getUpdates` again.
