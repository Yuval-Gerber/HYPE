# Hype

**A fully automated copy-trading bot for Solana, with a native desktop dashboard.**

Hype finds the most profitable trader wallets on Solana, watches them in real
time, and — when one of them buys a token — checks that token for scam risk,
copies the buy, and manages the exit on its own with take-profit / stop-loss
rules. Everything is visible and controllable from a PyQt6 desktop app and a
Telegram bot.

~14,000 lines of Python · asyncio + WebSockets · PyQt6 · SQLite · Solana / Jupiter

> Personal project. It trades the owner's own funds; it is not a product or
> financial advice. Built paper-trading-first: the simulator and the live
> executor share 100% of the strategy code.

---

## How it works

```
 Birdeye API ──▶  RANKER ──────▶ followed wallets (top N by realized PnL)
                                        │ watch
 Helius WebSocket ──▶ LIVE MONITOR ─────┤ buy detected
                                        ▼
                  SAFETY FILTERS ◀── STRATEGY (position sizing)
                  pass / fail            │ order
                                         ▼
                  EXECUTOR  ◀──────  POSITION MANAGER (TP / SL polling)
                  paper │ live           │ events
                        │                ▼
                  Jupiter swap     SQLite ──▶ Dashboard · Telegram · Investigation engine
```

1. **Rank** — pulls top Solana wallets by 7-day *realized* PnL, drops one-hit
   wonders (minimum trade count) and idle wallets, follows the top 15.
   Re-ranks every 4 hours.
2. **Watch** — one WebSocket connection subscribes to every followed wallet;
   each transaction is parsed and classified, and only **buys** become signals.
3. **Filter** — seven safety checks run *in parallel* before any money moves.
4. **Size** — 10% of current balance per trade, hard cap per trade, max open
   positions, and an untouchable SOL gas reserve.
5. **Execute** — paper fill (modeled slippage + fees) or a real Jupiter swap.
6. **Exit** — Hype never waits for the copied trader to sell; it polls prices
   and closes at its own take-profit / stop-loss.
7. **Police** — every trader builds a track record *on Hype*; underperformers
   are automatically benched for review.

---

## Features

### Trading engine
- **Wallet ranker** (Birdeye) — realized-PnL leaderboard with eligibility
  filters; ranking refresh never disturbs open positions.
- **Live monitor** (Helius) — `logsSubscribe` WebSocket, a parse queue with a
  worker so the read loop never blocks on HTTP, signature de-duplication, and
  automatic reconnect with backoff.
- **Paper / live executors behind one interface** — the engine, sizing, and
  position manager are identical in both modes; only the executor is swapped.
- **Live execution via Jupiter** — quote → build swap → sign →
  **`simulateTransaction`** → send. A transaction the RPC rejects in simulation
  is never broadcast. Slippage cap and per-trade spend cap enforced.
- **TP / SL position manager** — cached DexScreener price feed so polling many
  positions doesn't hammer the API; per-position manual "Sell now".
- **Rent reclaim** — closes empty token accounts after sells to recover the
  ~0.002 SOL of rent Solana locks per new token, which otherwise slowly bleeds
  the wallet.
- **Watchdog + keep-awake** — restarts the engine if it dies or stalls, and
  holds a macOS power assertion so system sleep can't freeze open positions.

### Scam / rug protection (runs before every buy)
| Check | Source |
|---|---|
| Mint authority revoked | Helius (on-chain) |
| Freeze authority revoked | Helius (on-chain) |
| Minimum liquidity (default $30k) | DexScreener |
| Top-10 holder concentration (default < 65%) | RugCheck / Helius |
| **Sellability simulation** — reverse Jupiter quote to reject honeypots | Jupiter |
| Minimum token age (optional) | DexScreener |
| RugCheck verdict not "danger" (best-effort) | RugCheck |

Every declined trade is logged with a human-readable reason, shown in the
dashboard's *Blocks* view.

### Trader investigation engine
A state machine that judges each copied trader by the results of the trades
Hype actually took from them:
- **Grace period** — no judgment before 5 copied trades.
- **Trigger** — 3 consecutive losses, or losses > wins → trader is auto-paused
  and flagged for review. Open positions keep running to their own exits.
- **Review actions** — *Second Chance* (probation), *Drop*, or *Keep Paused*.
- **Escalating probation** — a trader on probation is watched for the next 3
  trades; failing again raises their level (×2 → ×3 → …), shown as a badge.
  Optional auto-drop ceiling.

### Desktop dashboard (PyQt6)
- **Login screen + lock button** — password or Touch ID; locking blurs the UI.
- **Top bar + sidebar** — paper/live toggle (with confirmation), start/stop,
  paper and live balances side by side, connection lights for each service.
- **Positions** — open trades with live P&L, time held, TP/SL, trigger trader.
- **Performance** — real-time equity curve (pyqtgraph) with a marker for every
  buy/sell **color-coded by the trader who triggered it**, win rate, best/worst,
  fees, per-trader leaderboards. Paper and live tracked separately.
- **Traders** — ranked copy list, investigation review cards, manual
  add/remove, deterministic human-readable nicknames instead of 44-char keys.
- **Settings** — every threshold is config-driven and live-editable; no magic
  numbers in the logic.
- **Activity log** — real-time feed of every action; filter by category and
  calendar date, mark rows, export to CSV.
- **System** — engine status, effective config view, block reasons, one-click
  test paper trade, and the Panic Drain button.
- Packaged as a double-clickable macOS app with PyInstaller.

### Telegram bot
Owner-only chat (other chat IDs are rejected). Commands: `/status` (with live
health checks), `/positions`, `/pnl`, `/mode`, `/start`, `/stop`, `/panic`.
Alerts are noise-controlled: routine buys are silent; closes ping only on a
win of +20% or more, or any loss.

---

## Security model

This bot holds a hot wallet, so the design goal is **containment**:

- **No arbitrary-send code path.** There is exactly one outbound-transfer
  function, and its destination is always read from a single allowlisted
  "home wallet" in config — never from UI input, signals, or API data.
  Withdrawals and Panic Drain can only go there.
- **Three factors to change that address** — password + Touch ID + a secret
  nickname. The generic config-save path refuses to modify it.
- **Private key lives in the macOS Keychain**, released only after Touch ID /
  password, decrypted into memory once per session. Never in code, env files,
  logs, or the database. API keys are stored the same way.
- **Credentials stored as salted scrypt verifiers**, compared in constant time.
- **Self-built transactions only** — Hype never signs a transaction handed to
  it from outside, and always simulates before sending.
- **Panic Drain** — one button (or `/panic`) sells everything and sweeps the
  wallet to the home address.

No system is 100% safe; the point is that the funds at risk are limited and
there is no path that can redirect them.

---

## Engineering notes

- **Portable core.** Everything macOS-specific (Keychain, Touch ID, keep-awake)
  sits behind small interfaces (`KeyVault`, injectable authenticator), so the
  engine can run unchanged on a Linux server with a different secrets backend.
- **Concurrency.** asyncio engine + WebSocket monitor running alongside the Qt
  event loop; worker threads for blocking I/O; cross-thread calls marshalled
  through Qt signals; SQLite in WAL mode so the UI, engine, and Telegram thread
  can read concurrently.
- **Resilient HTTP.** A shared client with per-service rate limiting and
  exponential backoff on 429/5xx — required to live on free API tiers.
- **Performance.** Safety checks run in parallel (~40× faster than the first
  sequential version), which matters when copying a buy seconds after it lands.
- **Typed config.** Pydantic models validate every setting; TOML persistence;
  defaults documented in [SPEC.md](SPEC.md) §9.
- **Design doc first.** [SPEC.md](SPEC.md) is the full specification the
  project was built against, in verified phases (paper engine before live).

## Tech stack

Python 3.12 · asyncio · websockets · requests · PyQt6 · pyqtgraph · SQLite ·
Pydantic · solders · Jupiter Swap API · Helius RPC/WebSocket · Birdeye ·
DexScreener · RugCheck · macOS Keychain + LocalAuthentication (Touch ID) ·
PyInstaller

---

## Running it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python hype_app.py          # desktop app (starts in paper mode)
python -m hype              # headless bootstrap + status report
pyinstaller --noconfirm Hype.spec   # build dist/Hype.app
```

API keys (Birdeye, Helius, Jupiter, optional Telegram) are entered once and
stored in the Keychain — there is no `.env` file. Paper mode needs no wallet
and no funds.

Developer tools in [tools/](tools/): foundation smoke test
(`p1_smoketest.py`), live ranker + monitor runner (`p2_run.py`), safety-filter
checker for any token mint (`p3_check.py`), paper-engine runner and a scripted
simulator that exercises TP/SL and the investigation state machine
(`p4_run.py`, `p4_sim.py`).

## Project layout

```
hype/
  ranker.py, monitor.py   wallet ranking + live buy detection
  safety.py               rug / honeypot filters + sellability simulation
  data/                   Birdeye, Helius, Jupiter, DexScreener, RugCheck clients
  engine/                 executors (paper | live), sizing, positions, pricing,
                          investigation, rent reclaim, the single transfer path
  security/               KeyVault interface, Keychain backend, Touch ID, auth
  notify/                 Telegram transport + command router
  ui/                     PyQt6 dashboard (tabs, login, lock, top bar, toasts)
  config.py, db.py        typed config + SQLite schema
tools/                    smoke tests, simulators, setup helpers
SPEC.md                   full design specification
```

## Disclaimer

Memecoins are extremely volatile and most lose value. This software is a
personal engineering project, provided as-is, with no guarantee of profit.
Not financial advice.
