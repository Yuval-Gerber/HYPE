# Hype — Design Specification

> Complete design spec for a personal automated Solana copy-trading bot called **Hype**. It is built **in phases** (§10); each phase is verified before the next one starts.

---

## 0. DESIGN RULES

1. **Phased build.** Build in the phases defined in §10. Each phase ends with a verification pass: what was built, what to check, what "working correctly" looks like, and how to test it. The next phase starts only after the previous one is verified.
2. **Verify dependencies.** Package names, API endpoints, and library versions are re-checked at build time; anything that has changed since this spec is noted and resolved before use.
3. **Real money, single user.** This app trades real funds on an irreversible chain. Security (§6) is non-negotiable and overrides convenience.
4. **Paper mode first.** Live trading and the real wallet are built LAST among the engine phases (Phase 6), only after paper mode is validated. Telegram is built last overall (Phase 7).
5. **Everything is config-driven and editable** from the dashboard. No hardcoded magic numbers in logic — read them from the config (§9).
6. **Portable by design.** No Mac-specific paths or assumptions baked into core logic (except the Touch ID / Keychain layer, which must be cleanly isolated behind an interface so a cloud build can swap it for a password-only fallback). Goal: moving to a Linux cloud server later is copy-paste-and-run.

---

## 1. WHAT HYPE IS

Hype is a 100% automated crypto copy-trading bot for **Solana memecoins**. It:

1. **Ranks** the top-performing Solana trader wallets (by realized profit) via Birdeye.
2. **Watches** those wallets live via Helius and detects when they **buy** a token.
3. **Filters** the token for rug/scam risk before doing anything.
4. **Copies the buy** by swapping via Jupiter, sized as a % of Hype's own balance.
5. **Manages the exit** with its own take-profit / stop-loss (it does NOT wait for the trader to sell).
6. **Polices its traders** with an investigation system that benches underperformers.
7. **Reports** everything to the owner via a PyQt6 dashboard (and later a Telegram bot).
8. **Auto-deposits** profit to the owner's personal wallet on a schedule (once enabled).

**Owner:** single user (personal use only). **End-goal (long horizon):** scale to depositing $1,500/day to the owner's wallet — treated as a target reached by growing balance + proven edge over time, not a near-term guarantee.

---

## 2. CORE PRINCIPLES

- **100% automated** once running; owner intervention is via the dashboard, not required for trading.
- **Balance-aware:** Hype reads its own wallet balance and records its starting balance per session; all sizing and P&L are computed from that. No owner-entered capital amounts in logic.
- **Paper → Live:** identical logic both modes; only the executor differs (simulator vs Jupiter).
- **Contained security:** limited hot-wallet funds, no arbitrary-send code path, single allowlisted payout destination.
- **Editable:** every threshold below is a default, changeable live in the dashboard.

---

## 3. ARCHITECTURE OVERVIEW

```
            ┌─────────────┐   top wallets    ┌──────────────┐
 Birdeye →  │  RANKER     │ ───────────────→ │  WALLET LIST │
 API        └─────────────┘                  └──────┬───────┘
                                                     │ watch
            ┌─────────────┐   live buy event  ┌──────▼───────┐
 Helius  →  │  MONITOR    │ ───────────────→  │  SIGNAL BUS  │
 WS/gRPC    └─────────────┘                   └──────┬───────┘
                                                     │ token + trader
            ┌─────────────┐  pass/fail        ┌──────▼───────┐
            │ SAFETY      │ ◀──────────────── │  STRATEGY    │
            │ FILTERS     │ ──────────────→   │  (sizing)    │
            └─────────────┘                   └──────┬───────┘
                                                     │ buy order
            ┌─────────────┐                   ┌──────▼───────┐
            │ EXECUTOR    │  paper | live     │  POSITION    │
            │ (sim|Jupiter)│ ◀───────────────│  MANAGER     │ TP/SL
            └─────────────┘                   └──────┬───────┘
                                                     │ events
        ┌──────────────┬─────────────┬───────────────┼───────────────┐
        ▼              ▼             ▼               ▼               ▼
   INVESTIGATION   PAYOUT/SWEEP   PANIC DRAIN    DASHBOARD       TELEGRAM
     ENGINE        (→ MetaMask)   (→ MetaMask)   (PyQt6)         (Phase 7)
```

**Two wallets:**

- **Trading wallet** — Hype's own Solana hot wallet. Key in macOS Keychain, Touch ID gated. This is what trades.
- **Home wallet** — owner's MetaMask **Solana** address (Base58, NOT the 0x Ethereum address). Receive-only. The ONLY valid destination for payouts and panic drain. Hype never holds its key.

---

## 4. TECH STACK — REQUIRED REPOS & PACKAGES

> Versions/package names are re-checked at build time. Links are starting points.

### Solana core (Python)

- **solders** — Rust-backed Solana primitives (Keypair, Pubkey, VersionedTransaction): https://github.com/kevinheavey/solders
- **solana-py** — RPC client + SPL token: https://github.com/michaelhly/solana-py
- **base58** — key/address encoding (PyPI)

### Execution — Jupiter

- **Jupiter Developer Platform / API** (official, REST quote + swap, Swap V2 / Ultra, OCO TP-SL limit orders; single API key): https://developers.jup.ag
- **jupiter-python-sdk** (optional community wrapper — experimental; the default is calling the REST API directly with solders/solana-py): https://github.com/0xTaoDev/jupiter-python-sdk
- Priority fees: use `getRecentPrioritizationFees`, base on the **median** of recent blocks (not the max) to avoid fluke spikes.

### Data / scan

- **Birdeye Data Services API** — Wallet Leaderboard, Token Top-Traders (sort by realized PnL, 2–90d), Wallet PnL (realized/unrealized + trade counts). Docs: https://docs.birdeye.so (API key; free dev tier to start)
- **Helius** — Solana RPC + Webhooks + enhanced WebSockets for live wallet monitoring; upgrade to **LaserStream (gRPC)** + **Sender** (staked tx landing) for live mode: https://www.helius.dev (API key; free tier to start, paid speed tier at go-live)

### Safety / rug checks

- **RugCheck** — token risk API: https://rugcheck.xyz
- Plus direct on-chain checks (mint/freeze authority, liquidity, holder concentration) via Helius/Birdeye token APIs, and a **sellability simulation** via a Jupiter quote in the reverse direction.

### Dashboard (PyQt6, Phase 5)

- **PyQt6** — Qt6 bindings
- **PyQtDarkTheme** — theme engine; **use LIGHT mode**: https://github.com/5yutan5/PyQtDarkTheme
- **pyqt6-widgets-library** — 50+ polished prebuilt widgets (cards, tables, forms): https://github.com/mewada-madhusudan/pyqt6-widgets-library
- **24-Modern-Desktop-GUI** — modern tabbed GUI template + PyInstaller packaging into a Mac app: https://github.com/KhamisiKibet/24-Modern-Desktop-GUI
- **QDarkStyleSheet** — backup stylesheet (light/dark): https://github.com/ColinDuquesnoy/QDarkStyleSheet
- **pyqtgraph** — fast real-time charts for the annotated equity curve: https://github.com/pyqtgraph/pyqtgraph

### Auth & secrets (security layer)

- **pyobjc-framework-LocalAuthentication** — Touch ID + password auth (PyPI; actively maintained)
- **VerifyOwner** — simple Touch ID wrapper: https://github.com/Aliebc/VerifyOwner
- **python-touch-id** — Touch ID reference: https://github.com/lukaskollmer/python-touch-id
- **keychain-fingerprint** — reference for Touch-ID-gated Keychain access: https://github.com/dss99911/keychain-fingerprint
- **keyring** — store/retrieve secrets in macOS Keychain (PyPI)

### Telegram (Phase 7)

- **python-telegram-bot**: https://github.com/python-telegram-bot/python-telegram-bot

### Reference architecture (for patterns only)

- **Solana-Copy-trading-bot** — wallet-monitoring + multi-DEX execution patterns: https://github.com/ChainInsighter/Solana-Copy-trading-bot

### Utility

- requests / aiohttp / websockets, pydantic (config models), SQLite (stdlib `sqlite3` or SQLAlchemy/SQLModel), PyInstaller (packaging).

---

## 5. SUBSYSTEM SPECS

### 5.1 Wallet Ranker (Birdeye)

- Pull top Solana wallets ranked by **7-day realized PnL** (default window, editable).
- Eligibility filters: **min trade count ≥ 10** (avoid one-hit wallets), exclude wallets with mostly unrealized (illiquid-bag) PnL.
- Follow the **top 15** eligible wallets (editable).
- Refresh ranking every **4 hours** (editable). New qualifying wallets can be added; dropped wallets are removed from active copying (open positions unaffected).

### 5.2 Live Monitor (Helius)

- Subscribe to each followed wallet's transactions (webhooks / enhanced WebSocket; LaserStream gRPC in live mode for speed).
- Detect **buy** events (token in, SOL/USDC out). Emit a signal: `{trader_wallet, token_mint, timestamp}`.
- We copy **buys only**; the trader's own sells are ignored (Hype runs its own exits).

### 5.3 Safety Filters (run BEFORE every buy; fail = skip)

- Mint authority **revoked** (required).
- Freeze authority **revoked** (required).
- Min liquidity **≥ $30,000** (editable).
- Top-10 holder concentration **< 65%** (editable).
- **Sellability simulation**: get a reverse Jupiter quote (token → SOL) to confirm the token can actually be sold; reject honeypots.
- Optional min token age (editable).
- RugCheck verdict not "danger" (if API available).

### 5.4 Executor (paper | live)

- Single `Executor` interface with two implementations:
  - **PaperExecutor** — simulates fills at current quote price incl. estimated slippage + fees; no chain interaction.
  - **LiveExecutor** — builds + signs + sends a Jupiter swap; never signs externally-supplied transactions; applies slippage cap and median-based priority fee; `simulateTransaction` before send.
- Mode chosen by the dashboard **paper/live toggle**.

### 5.5 Position Sizing

- Per trade = **10% of total balance**, capped at **$5,000** per trade (editable).
- Max **10 open positions** at once (editable). If a new signal would exceed the cap, **skip it**.
- Always keep a **gas reserve** (e.g., 0.05 SOL, editable) untouched so Hype can always pay fees.
- Balance is read live from the wallet (live) or the simulated balance (paper, default **$1,000**).

### 5.6 Exit Logic

- Default **Take-Profit +10%**, **Stop-Loss −15%** (editable, live).
- On reaching TP or SL, sell the full position via the Executor.
- (Optional later: trailing TP — not v1.)

### 5.7 Trader Investigation Engine (state machine)

Each followed trader has a record built **on Hype** (outcomes of copied trades).

- **Grace period:** trader is not judged until they have **≥ 5 copied trades** (editable).
- **Investigation trigger (level 1):** after grace, if **3 consecutive losses** OR **cumulative losses > wins** → trader goes **UNDER INVESTIGATION**.
- On trigger: **auto-pause** that trader (stop copying new buys from them; existing positions run to their own TP/SL), alert owner (dashboard + Telegram).
- **Owner review actions:** `Second Chance` | `Drop` | `Keep Paused`.
  - **Second Chance** → reset that trader's win/loss counter, enter **PROBATION** at next level.
  - **Drop** → remove from active list (archived); owner can manually re-add later as fresh.
  - **Keep Paused** → benched until owner decides.
- **Probation rule (level ≥ ×2):** watch the **next 3 trades**; if **2+ losses in those 3** (losing more than winning) → back under investigation, **level increments** (×2 → ×3 → … → ×n). Show the level as a badge on the trader's card.
- **Auto-drop ceiling:** OFF by default (chances climb to n). If enabled, auto-drop at a configurable level (e.g. ×4).

### 5.8 Payout / Auto-Deposit (→ home wallet)

- **Disabled by default.** Owner enables it explicitly.
- When enabled: every **24h**, sweep a **fixed owner-set amount** (default asset **USDC**) from the trading wallet to the **home wallet** (MetaMask Solana address).
- The fixed daily amount is a dial the owner raises as balance/track record grow toward the $1,500/day goal.
- If balance < the fixed amount on a given day, sweep what's safely available above the gas reserve + keep enough working capital (define a `min_working_balance` guard; skip/partial-sweep rather than draining).

### 5.9 Panic Drain

- One **dashboard button** + optional **auto-trigger** on anomaly detection, that immediately sweeps **everything** (above gas reserve) from the trading wallet to the **home wallet** — the only valid destination.

### 5.10 Balance Awareness

- On entering a mode, record `starting_balance` (paper = $1,000; live = current wallet balance) as the P&L baseline + equity-curve origin.
- Continuously read live balance for sizing and reporting.

---

## 6. SECURITY MODEL (HARD REQUIREMENTS)

1. **Private key** stored in **macOS Keychain**, released only after **Touch ID / password**. Never in code, env files, logs, or plaintext. Decrypted into memory once at authorized startup; isolate all key handling behind a single `KeyVault` interface (so cloud can swap to a password-only / OS-secret backend).
2. **No arbitrary-send capability.** The codebase contains **no function** that sends funds to an arbitrary address. The ONLY outbound transfer path targets the single allowlisted home wallet. There must be no way for any input/signal/config-injection to redirect funds.
3. **Changing the home (payout) address** requires THREE factors: **password + Touch ID + an owner-chosen secret nickname**.
4. **Hot-wallet hygiene:** trading wallet holds only working funds; owner keeps the main stash in MetaMask. Dashboard shows the trading wallet address + QR for the owner to fund it; Hype auto-detects deposits.
5. **Self-built transactions only.** Hype constructs swap transactions itself from a token mint + amount via Jupiter; it never signs externally-provided transactions. Always `simulateTransaction` before sending. Enforce slippage + per-trade spend caps.
6. **Login screen** (password or Touch ID) on launch. **Lock button** on the top bar → blurs the UI and locks until password/Touch ID. Settings, mode switches, and payout-address changes are all auth-gated.
7. **Honest note (also reflected in code comments):** no system is 100% safe; the model is _containment_ — limited funds at risk, no arbitrary-send path, scam-token filters, panic drain to the one safe address.

---

## 7. DASHBOARD SPEC (PyQt6 — Phase 5)

- **Style:** LIGHT theme (PyQtDarkTheme light mode), **tabbed layout** (no long scrolling — each panel is a tab), packaged as a Mac app via PyInstaller.
- **Login screen:** password or Touch ID before access.
- **Top bar (always visible):** Paper/Live toggle (color-coded, confirm prompt on→Live) · Start/Pause/Stop · connection lights (Birdeye·Helius·Jupiter·Wallet·Telegram) · live balance (paper & live shown separately) · **Lock button** (blur + auth to unlock) · **Panic Drain** button.

**Tabs:**

1. **Positions** — open trades: token, trigger trader, entry, current price, live % P&L, time held, TP/SL; per-position "Sell now"; color-coded.
2. **Performance** — **annotated equity curve** (pyqtgraph) with a marker on every buy/sell, **color-coded by which trader triggered it**; today's P&L, total P&L, win rate, # trades, best/worst, fees; per-trader breakdown. Paper & live tracked separately.
3. **Traders** — auto-ranked copy list (realized PnL, win rate, trade count, last active); **investigation review cards** with level badges (×2, ×3…) and Second Chance / Drop / Keep Paused; enable/disable + manual add/remove; refresh-ranking control.
4. **Settings** — every value in §9, live-editable. Includes the home-wallet address field (3-factor gated to change) and the funding address + QR display.
5. **Activity Log** — real-time feed of every action (scan hit, buy, sell, TP, SL, investigation, mode switch, error), filterable + exportable.
6. **System** — RPC latency, API usage, uptime, last heartbeat, SOL gas balance.

---

## 8. TELEGRAM BOT SPEC (Phase 7 — built last)

- Full **control + alerts** from the phone.
- **Alerts:** position opened/closed (with trader + P&L), daily P&L summary, trader added/paused/investigated, errors, panic-drain, kill events.
- **Commands:** `/status`, `/pnl`, `/positions`, `/traders`, `/pause`, `/resume`, `/stop`, `/panic`, `/mode` (read), `/settings` (read). Auth the chat to the owner only.
- Built and attached only **after paper mode is validated**.

---

## 9. CONFIG & DEFAULTS (all editable in dashboard)

| Key                             | Default                         |
| ------------------------------- | ------------------------------- |
| mode                            | paper                           |
| paper_starting_balance_usd      | 1000                            |
| per_trade_pct                   | 10%                             |
| per_trade_cap_usd               | 5000                            |
| max_open_positions              | 10                              |
| gas_reserve_sol                 | 0.05                            |
| take_profit_pct                 | +10%                            |
| stop_loss_pct                   | −15%                            |
| birdeye_rank_window             | 7d realized PnL                 |
| wallet_min_trades               | 10                              |
| follow_top_n                    | 15                              |
| rank_refresh_hours              | 4                               |
| filter_mint_authority_revoked   | required                        |
| filter_freeze_authority_revoked | required                        |
| filter_min_liquidity_usd        | 30000                           |
| filter_max_top10_holders_pct    | 65                              |
| filter_sellability_sim          | required                        |
| slippage_bps                    | 200 (2%)                        |
| grace_period_trades             | 5                               |
| invest_consecutive_losses       | 3                               |
| invest_losses_gt_wins           | on                              |
| probation_window_trades         | 3                               |
| probation_losses_to_trip        | 2                               |
| auto_drop_ceiling               | off                             |
| payout_enabled                  | false                           |
| payout_interval_hours           | 24                              |
| payout_fixed_amount_usd         | (owner-set)                     |
| payout_asset                    | USDC                            |
| home_wallet_address             | (owner-set, 3-factor to change) |

---

## 10. BUILD PHASES (each verified before the next)

- **P1 — Foundation:** project scaffold, config system (§9), SQLite schema, logging, `KeyVault`/Keychain layer, secrets handling. _Check:_ app runs, config loads + persists, secrets stored in Keychain (not plaintext), Touch ID prompt works.
- **P2 — Data layer (read-only, no trading):** Birdeye ranker + Helius live monitor. _Check:_ it prints the current top-15 wallets with stats, and prints their live buys in real time.
- **P3 — Safety filters:** rug/honeypot checks + sellability simulation. _Check:_ feed known-good and known-bad token mints; verify correct pass/fail verdicts.
- **P4 — Paper engine:** PaperExecutor, sizing, TP/SL position manager, investigation engine, equity tracking. _Check:_ simulated trades open/close on real live signals, equity curve updates, TP/SL fire correctly, investigation triggers + probation escalation behave per §5.7.
- **P5 — Dashboard (PyQt6):** light/tabbed UI, all tabs (§7), login + lock/blur, paper/live toggle, annotated equity curve, panic-drain button (wired in P6). _Check:_ every panel works in paper mode; settings edits apply live.
- **P6 — Live + wallet:** real Solana trading wallet via Keychain, LiveExecutor (Jupiter) with simulate-before-send + slippage/priority handling, funding address + QR + balance auto-detect, allowlisted payout, panic drain, 3-factor address change. _Check:_ a tiny real test trade verified on-chain (Solscan), sellability honored, payout sweep to MetaMask Solana address verified, panic drain verified.
- **P7 — Telegram:** full control + alerts (§8). _Check:_ every command works from the phone; alerts arrive.
- **P8 — Cloud (later):** migrate engine to a Linux VPS (24/7), swap KeyVault to a server-secure backend with password auth, keep dashboard talking to the remote engine. _Check:_ runs with Mac closed; controls still work.

---

## 11. PAPER vs LIVE

- Identical logic; only the Executor differs. Paper uses a simulated $1,000 balance and simulated fills (with modeled slippage/fees). Live reads the real wallet balance. Owner flips to Live manually, only when satisfied with paper results.

## 12. CLOUD PORTABILITY

- Keep all OS-specific code (Keychain, Touch ID) behind interfaces. Core engine must run unchanged on Linux. Config-driven endpoints and paths. Target: lift-and-shift to a VPS in Phase 8.

## 13. HONEST RISK NOTES / NON-GOALS

- Memecoins are extremely volatile (can pump and rug fast). Win rate is bounded by the wallets copied and the filters; expect losing trades — the system relies on disciplined exits and the investigation engine, not on never losing.
- $1,500/day is a long-horizon target requiring substantial balance + sustained edge; the payout dial scales with proven performance.
- This is a personal-use tool trading the owner's own funds, who accepts the risk including total loss of deposited funds. Not financial advice.

---

\*End of spec.
