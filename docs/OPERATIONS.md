# Operations — running `brief` on a schedule (macOS / launchd)

`flight-deals brief` is the monitoring loop (UPGRADE-PLAN §6): it runs every
*due* saved search, diffs confirmed prices against the alert state machine,
fires exactly-once Telegram alerts, prunes stale state, and prints one digest
envelope. On a Mac the scheduler is **launchd** (no cron).

A single `flock` guarantees only one `brief` runs at a time — a second
concurrent instance exits 1 with `brief: already running`, so overlapping
launchd wake-ups (or a manual run mid-cron) can never double-send.

---

## 1. Prerequisites

- The package installed editable in a venv: `pip install -e '.[dev]'`.
- `flight-deals --help` works offline (no network at import).
- Telegram secrets available **as environment variables** (never in a config
  file — Global Constraint 8):
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`

Verify sending works before automating:

```sh
flight-deals brief --dry-run     # prints the digest chunks, sends nothing
TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… flight-deals brief --send --all
```

## 2. Create some watches

```sh
# Single route, alert under €150, checked daily:
flight-deals watch add BUD-CFU --months 2026-08,2026-09 --nights 4-7 --max-price 150

# A category watch with a budget threshold:
flight-deals watch add --where "seaside & (italy | greece)" --month 2026-08 \
    --nights 5-8 --budget 120

flight-deals watch list          # confirm
flight-deals searches due        # what would run right now
```

Each saved search lives in `data/searches/<name>.yaml` — human-readable, atomic
writes, versioned. Run stamps live in `data/searches/.runs.json`. **Don't
hand-edit these while `brief` may be running** — use the `searches`/`watch`
verbs.

## 3. Install the launchd job

The template is `deploy/launchd/com.flightdeals.brief.plist`. It fires at
**08:30 / 13:30 / 19:30** local time; `brief` decides which saved searches are
actually due at each wake-up.

```sh
HOME_DIR="$(pwd)"                         # project root (has pyproject.toml)
BIN="$HOME_DIR/.venv/bin/flight-deals"
DEST="$HOME/Library/LaunchAgents/com.flightdeals.brief.plist"

mkdir -p "$HOME_DIR/data/logs"
sed -e "s#__FLIGHT_DEALS_HOME__#$HOME_DIR#g" \
    -e "s#__FLIGHT_DEALS_BIN__#$BIN#g" \
    deploy/launchd/com.flightdeals.brief.plist > "$DEST"

launchctl load "$DEST"                    # register (RunAtLoad is false → no immediate send)
launchctl list | grep com.flightdeals     # confirm it's registered
```

### Injecting secrets

The plist must **not** contain secrets in git. Two options:

- **1Password (preferred):** change `ProgramArguments` to run under `op run`,
  e.g. `op run --env-file=".env.op" -- flight-deals brief --send`, where
  `.env.op` maps `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` to `op://…` refs.
- **EnvironmentVariables (single-user Mac):** uncomment the `TELEGRAM_*` keys in
  the installed copy of the plist, then `chmod 600 "$DEST"`. The secret now
  lives only in your `~/Library/LaunchAgents` copy, never in the repo.

## 4. Manual run

```sh
flight-deals brief               # run due searches, print envelope, no Telegram
flight-deals brief --all         # ignore schedules, run every saved search
flight-deals brief --send        # run + Telegram (only messages if there's news)
flight-deals brief --dry-run     # preview the digest chunks, offline
```

`--send` only messages when there's something to report (an alert or a mover),
so an hourly wake-up with no news stays silent while a real drop is never
missed.

## 5. Logs

```sh
tail -f data/logs/brief.out.log   # the JSON envelope of each run
tail -f data/logs/brief.err.log   # warnings, provider status, send failures
```

A failed Telegram send logs the API response body to stderr and makes `brief`
exit non-zero, so a broken token surfaces in `brief.err.log` rather than
failing silently.

Rotate by truncating when large: `: > data/logs/brief.out.log`.

## 6. Uninstall

```sh
launchctl unload "$HOME/Library/LaunchAgents/com.flightdeals.brief.plist"
rm "$HOME/Library/LaunchAgents/com.flightdeals.brief.plist"
```

Saved searches and history under `data/` are untouched by uninstalling — remove
individual watches with `flight-deals watch rm <name>`.

## 7. State files (all under `data/`, atomic + versioned)

| File | Role |
|---|---|
| `searches/<name>.yaml` | one saved search / watch (spec + schedule + alert) |
| `searches/.runs.json` | last-run stamp per search (drives `due`) |
| `alert_state.json` | alert state machine (per search/route/month) |
| `deals/<deal_id>.jsonl` | append-only price observations (`check` + movers) |
| `price_history.csv` | append-only price-context store (typical-price stats) |
| `locks/brief.lock` | the `flock` that makes `brief` single-instance |

`brief` prunes past-dated snapshots, expired cache entries, stale run stamps and
expired alert entries on every run, so the git-versioned `data/` dir stays
bounded.
