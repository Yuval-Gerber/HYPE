"""Phase 4 deterministic simulation — proves the paper engine logic end to end
WITHOUT waiting on the live market.

Uses a scripted price feed and skips the network safety filters so we can drive
exact price moves and trade outcomes:
  A. TP fires on a +move, SL fires on a −move; equity/P&L update correctly.
  B. Sizing rules (§5.5): max-open, gas reserve, dedupe.
  C. Investigation state machine (§5.7): grace → trigger → second chance →
     probation → trip → escalate ×2→×3; paused traders aren't copied;
     auto-drop ceiling.

Run:  python tools/p4_sim.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_scratch = tempfile.mkdtemp(prefix="hype_p4_")
os.environ["HYPE_HOME"] = _scratch
os.environ["HYPE_CONFIG"] = os.path.join(_scratch, "config.toml")
os.environ["HYPE_DB"] = os.path.join(_scratch, "hype.db")

from hype import db  # noqa: E402
from hype.config import load_config  # noqa: E402
from hype.engine.engine import PaperEngine  # noqa: E402
from hype.engine.executor import PaperExecutor  # noqa: E402
from hype.engine.pricing import ScriptedPriceFeed  # noqa: E402
from hype.engine.sizing import decide_size  # noqa: E402
from hype.models import BuySignal  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))


def make_engine(cfg, conn, feed):
    ex = PaperExecutor(feed, slippage_bps=cfg.trading.paper_slippage_bps,
                       fee_bps=cfg.trading.paper_fee_bps)
    return PaperEngine(conn, cfg, ex, feed, skip_filters=True)


def open_and_move(engine, feed, conn, trader, token, *, win: bool):
    """Open a position then move price to trigger TP (win) or SL (loss)."""
    feed.set(token, 1.0)
    out = engine.handle_signal(BuySignal(trader, token, f"sig_{token}", "2026-06-29T00:00:00+00:00"))
    if not out.accepted:
        return out, None
    ep = conn.execute("SELECT entry_price FROM positions WHERE id=?", (out.position_id,)).fetchone()["entry_price"]
    feed.set(token, ep * 1.12 if win else ep * 0.82)
    closes = engine.manage_once()
    return out, (closes[0] if closes else None)


def main() -> int:
    # ---------- A. TP / SL + equity ----------
    cfg = load_config()
    conn = db.init_db()
    feed = ScriptedPriceFeed()
    engine = make_engine(cfg, conn, feed)

    out, close = open_and_move(engine, feed, conn, "TraderAAA", "TOKEN_TP", win=True)
    check("buy opens a position", out.accepted, out.reason)
    check("TP fires on +12% move", close is not None and close.reason == "tp",
          f"reason={getattr(close,'reason',None)} pnl={getattr(close,'realized_pnl_pct',0):.1f}%")
    check("TP trade is a win", close is not None and close.win)

    out, close = open_and_move(engine, feed, conn, "TraderAAA", "TOKEN_SL", win=False)
    check("SL fires on -18% move", close is not None and close.reason == "sl",
          f"reason={getattr(close,'reason',None)} pnl={getattr(close,'realized_pnl_pct',0):.1f}%")
    check("SL trade is a loss", close is not None and not close.win)

    eq = engine.account.equity(engine.session_id, feed)
    start = cfg.trading.paper_starting_balance_usd
    check("equity reflects net P&L", abs(eq.equity_usd - start) > 0 and eq.realized_pnl_usd != 0,
          f"equity=${eq.equity_usd:,.2f} realized=${eq.realized_pnl_usd:+,.2f}")
    snaps = conn.execute("SELECT COUNT(*) c FROM equity_snapshots WHERE marker IS NOT NULL").fetchone()["c"]
    check("equity-curve markers recorded", snaps >= 4, f"{snaps} markers")
    conn.close()

    # ---------- B. Sizing rules (§5.5) ----------
    cfg2 = load_config()
    check("size: 10% of equity",
          abs(decide_size(equity_usd=1000, available_cash_usd=1000, open_positions=0, cfg=cfg2).usd_amount - 100) < 1e-6)
    check("size: capped at per_trade_cap",
          decide_size(equity_usd=1_000_000, available_cash_usd=1_000_000, open_positions=0, cfg=cfg2).usd_amount == cfg2.trading.per_trade_cap_usd)
    check("size: rejects at max open positions",
          not decide_size(equity_usd=1000, available_cash_usd=1000, open_positions=cfg2.trading.max_open_positions, cfg=cfg2).approved)
    gas = cfg2.trading.gas_reserve_sol * cfg2.engine.gas_reserve_sol_price_usd
    check("size: protects gas reserve",
          not decide_size(equity_usd=gas, available_cash_usd=gas - 0.01, open_positions=0, cfg=cfg2).approved,
          f"gas reserve=${gas:.2f}")

    # dedupe via engine
    conn = db.init_db()
    feed = ScriptedPriceFeed(); feed.set("DUP", 1.0)
    engine = make_engine(cfg2, conn, feed)
    o1 = engine.handle_signal(BuySignal("TraderD", "DUP", "s1", "2026-06-29T00:00:00+00:00"))
    o2 = engine.handle_signal(BuySignal("TraderD", "DUP", "s2", "2026-06-29T00:00:00+00:00"))
    check("dedupe: no 2nd position in same token", o1.accepted and not o2.accepted, o2.reason)
    conn.close()

    # ---------- C. Investigation state machine (§5.7) ----------
    cfg3 = load_config()
    conn = db.init_db()
    feed = ScriptedPriceFeed()
    engine = make_engine(cfg3, conn, feed)
    inv = engine.investigation
    T = "TraderINV"

    # 5 wins to clear the grace period without tripping
    for i in range(cfg3.investigation.grace_period_trades):
        open_and_move(engine, feed, conn, T, f"G{i}", win=True)
    st = inv.load(T)
    check("grace: still active after 5 wins", st.state == "active" and st.grace_trades_done >= 5,
          f"state={st.state} grace={st.grace_trades_done}")

    # 3 consecutive losses → triggers investigation
    for i in range(cfg3.investigation.invest_consecutive_losses):
        open_and_move(engine, feed, conn, T, f"L{i}", win=False)
    st = inv.load(T)
    check("trigger: 3 consecutive losses → under_investigation",
          st.state == "under_investigation" and st.level == 1, f"state={st.state} level={st.level}")

    # paused trader is not copied
    o = engine.handle_signal(BuySignal(T, "BLOCKED", "sx", "2026-06-29T00:00:00+00:00"))
    check("investigation: under-investigation trader not copied", not o.accepted, o.reason)

    # Second Chance → probation ×2
    ev = inv.second_chance(T)
    st = inv.load(T)
    check("second chance → probation ×2", st.state == "probation" and st.level == 2,
          f"state={st.state} level={st.level}")

    # 2 probation losses → trip back to investigation (level stays 2)
    open_and_move(engine, feed, conn, T, "P0", win=False)
    open_and_move(engine, feed, conn, T, "P1", win=False)
    st = inv.load(T)
    check("probation trip: 2 losses → under_investigation", st.state == "under_investigation",
          f"state={st.state} level={st.level}")

    # Second Chance again → ×3 (escalation)
    ev = inv.second_chance(T)
    st = inv.load(T)
    check("escalation: next second chance → probation ×3", st.state == "probation" and st.level == 3,
          f"level={st.level}")

    # probation pass: survive window with <2 losses → back to active
    win_seq = ["P3a", "P3b", "P3c"]
    for tk in win_seq:
        open_and_move(engine, feed, conn, T, tk, win=True)
    st = inv.load(T)
    check("probation pass: survive window → active", st.state == "active", f"state={st.state}")

    # owner actions: drop / keep_paused
    inv.drop(T); check("owner: drop → dropped", inv.load(T).state == "dropped")
    inv.readd(T); check("owner: re-add → fresh active level 0",
                        inv.load(T).state == "active" and inv.load(T).level == 0)

    # auto-drop ceiling
    cfg4 = load_config(); cfg4.investigation.auto_drop_ceiling = 3
    engine4 = PaperEngine(conn, cfg4, PaperExecutor(feed), feed, skip_filters=True)
    engine4.ensure_trader("CeilT")
    eng_inv = engine4.investigation
    # force to under_investigation level 2, then second chance → would be 3 >= ceiling → auto-drop
    s = eng_inv.load("CeilT"); s.state = "under_investigation"; s.level = 2; eng_inv.save(s)
    ev = eng_inv.second_chance("CeilT")
    check("auto-drop ceiling: ×3 ceiling drops instead of probation",
          ev.kind == "auto_dropped" and eng_inv.load("CeilT").state == "dropped", ev.detail)
    conn.close()

    # ---------- report ----------
    print()
    width = max(len(n) for _, n, _ in results)
    passed = 0
    for ok, name, detail in results:
        mark = "\033[92m[ok]\033[0m" if ok else "\033[91m[XX]\033[0m"
        line = f"  {mark} {name.ljust(width)}"
        if detail:
            line += f"   {detail}"
        print(line)
        passed += ok
    print(f"\n  {passed}/{len(results)} checks passed.")
    print(f"  scratch: {_scratch}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
