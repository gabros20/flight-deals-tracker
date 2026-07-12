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

### Environment variables

All optional except the Telegram secrets (only needed for `--send`). Config-file
values are overridden by these; secrets are env-only (never written to disk).

| Variable | Role | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (send) | — |
| `TELEGRAM_CHAT_ID` | Telegram chat id (send) | — |
| `FLIGHT_DEALS_LOG` | Log level (`DEBUG`/`INFO`/`WARNING`/…) | `WARNING` |
| `FLIGHT_DEALS_HOME` | Project root for state/data paths | auto-detected |
| `FLIGHT_DEALS_DATA_DIR` | Data dir (relative to home) | `data` |
| `FLIGHT_DEALS_DEFAULT_ORIGIN` | Default origin IATA | `BUD` |
| `FLIGHT_DEALS_CURRENCY` | Display currency | `EUR` |
| `FLIGHT_DEALS_HTTP_RATE` | Shared HTTP rate limit (req/s) | `1.0` |
| `FLIGHT_DEALS_MAX_WORKERS` | Worker pool size | `8` |
| `FLIGHT_DEALS_CACHE_TTL_HOURS` | Legacy FlightCache TTL (hours) | `0.25` |

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

## 4a. Agentic review (optional)

Beside the deterministic `brief` loop there is an optional *agentic* periphery
(SEARCH-DESIGN §6): saved searches that carry an `agent_prompt` are meant to be
reviewed by a reasoning agent rather than just diffed against a price threshold.
The loop is:

```sh
flight-deals searches due --agentic          # due searches carrying an agent_prompt
# for each NAME it prints:
flight-deals wake "$NAME"                     # read-only bundle: spec + agent_prompt
                                              #   + last_result + history + allowed_moves
# feed that JSON bundle to your agent, which may persist a variation with
# `flight-deals searches add --name <name> --spec -` (idempotent).
```

`wake` reads saved state only — no network — so it is cheap to skip on a day
with nothing due. A ready-to-cron driver is `deploy/agentic-wake.sh`:

```sh
AGENT_CMD='hermes run --skill flight-deals-review' deploy/agentic-wake.sh
```

`AGENT_CMD` is **user-specific** — it is however you invoke your agent (a
`hermes …` or `claude …` CLI call, a script, etc.) reading the wake bundle on
stdin. The example ships as a placeholder; the script just echoes the bundle if
`AGENT_CMD` is unset, so you can dry-run the loop before wiring an agent in.
Schedule it with launchd exactly like `brief` (§3), pointing at this script.

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

## 8. Data refresh (out-of-band — never touches the request path)

Two precomputed data tables are refreshed by manual/cron scripts, never from a
live search (Global Constraint 9 / 10). Both write atomically and leave the
existing file untouched on failure.

| Script | Writes | Cadence | Source |
|---|---|---|---|
| `scripts/refresh_fx.py` | `data/fx_rates.json` | weekly | frankfurter.app (ECB) |
| `scripts/refresh_ground.py` | `data/ground_matrix.json` | monthly **or after any registry change** | OSRM public `/table` + `/route` |

```bash
# Preview without writing (fetches + prints stats):
.venv/bin/python scripts/refresh_ground.py --dry-run

# Refresh the computed open-jaw ground matrix in place:
.venv/bin/python scripts/refresh_ground.py

# ...also refine durations with real Transitous/MOTIS timetables where
# coverage exists (adds a third pure pass + a fourth hybrid pass, ~4 min — see below):
.venv/bin/python scripts/refresh_ground.py --transit
```

`refresh_ground.py` makes **one** OSRM public `/table` request for the full
registry coordinate set (under the 100-location public limit) and derives the
open-jaw ground estimates (model in `SEARCH-DESIGN.md` §3), then a second pass
of **one `/route` request per kept pair** (~39 pairs) to detect ferry crossings
(§7b). Paced at the house-rule ~1 req/s, the `/route` pass alone takes ~N
seconds for N kept pairs (~39s today) — call it **~1 minute** end to end
including the `/table` request. **Run it whenever `data/destinations.json`
gains, removes, or moves an airport** — otherwise a new airport has no computed
open-jaw pairs. If OSRM is down/refuses, the script exits non-zero and the
committed matrix is left unchanged (the planner keeps serving the curated
pairs plus the last good matrix).

With `--transit` a **third pass** (Task 13) refines each kept pair's modeled
duration with a real Transitous/MOTIS scheduled itinerary where coverage exists:
two representative departures per pair (next Tuesday ≥14 days out, 10:00 &
15:00 UTC), paced ~1 req/s — ~38 pairs × 2 ≈ **~2 minutes** on top of the
table+route passes. Coverage is **sparse** (most airports have no on-site
transit stop in Transitous's feeds — the 2026-07-12 live run refined 2 of 38
pairs); pairs with no scheduled itinerary keep their modeled estimate
(`transit: "no_coverage"`). A whole-service Transitous outage never invalidates
the matrix: the script writes the table+route results and exits non-zero for the
transit pass only. This pass is optional — plain `refresh_ground.py` (no
`--transit`) is unchanged.

`--transit` also runs a **fourth pass** (Task 14, city-anchor hybrid): for each
pair the pure third pass left at `no_coverage`, it re-queries CITY-CENTER anchor
→ CITY-CENTER anchor for the intercity line-haul and adds modeled airport-access
pads (`hybrid_minutes = pad_a + line-haul + pad_b`). Same two slots, same ~1
req/s pacing — ~36 no_coverage pairs × 2 ≈ **~72 extra requests (~2 minutes)** on
top of the third pass. Refined pairs surface as `estimate_basis:
"scheduled-hybrid"` (the line-haul is scheduled, the pads are modeled, so the `~`
stays on duration). Whole-pass failure isolation is identical to the third pass
(the matrix stays valid; exits non-zero for the hybrid pass only).

Cron example (monthly, 1st at 05:00):

```cron
0 5 1 * *  cd /path/to/flight-deals-tracker && .venv/bin/python scripts/refresh_ground.py
```
