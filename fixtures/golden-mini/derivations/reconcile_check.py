"""C0.3b reconciliation: independent third computation of the golden-mini
expected outputs under the PINNED rules, comparison against calc-A/calc-B,
and emission of the reconciled expected files (JCS-canonical).

All arithmetic exact: ints + Fraction. No floats anywhere.

HISTORICAL ARTIFACT — pinned to the pre-C0.3d TWO-STAMP pack
(content_hash sha256:cc80931d… / sha256:ea4828fa…). At C0.3d (M0 audit
round 2) the pack's funding switched to a 4h interval (four stamps); this
script's assertions are only valid against the old pack bytes and it is NOT
re-runnable against the current pack. Do not "fix" it: it is the verbatim
C0.3b record. Current recompute path: emit_expected_v1.py +
reconciliation.md section 8.
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, "/Users/leonliu/Desktop/TradeEvolve")
from spec.canonical import canonical_bytes  # noqa: E402

ROOT = Path("/Users/leonliu/Desktop/TradeEvolve/fixtures/golden-mini")

TICK = 10000            # micro per tick
QTY_STEP = 100000       # 0.001 BTC in 1e-8
TAKER = 45000           # 1e-8
HALF_SPREAD = 50000     # 1e-8
PART_CAP = Fraction(10000000, 10**8)  # 10%
MM = 16666667           # 1e-8
LIQ_PEN = 1000000       # 1e-8 (1%)
LEV_CAP = 30000         # 1e-4
START_NAV = 10**10
E8 = 10**8


def ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def floor_frac(f: Fraction) -> int:
    return f.numerator // f.denominator


def ceil_frac(f: Fraction) -> int:
    return -((-f.numerator) // f.denominator)


def load(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def liq_price(entry_ticks: int, pos_sign: int, lev_1e4: int) -> int | None:
    """Pinned: maintenance-crossing formula on entry px + target leverage.
    long: ceil(E*(L-1)/L / (1-mm)); short: floor(E*(L+1)/L / (1+mm)).
    Long with lev<=1x: clamp to 0 (unreachable)."""
    if pos_sign > 0:
        num = Fraction(entry_ticks) * Fraction(lev_1e4 - 10000, lev_1e4)
        if num <= 0:
            return 0
        return ceil_frac(num / Fraction(E8 - MM, E8))
    num = Fraction(entry_ticks) * Fraction(lev_1e4 + 10000, lev_1e4)
    return floor_frac(num / Fraction(E8 + MM, E8))


def dist_1e8(px_ticks: int, liq: int, pos_sign: int) -> int:
    if pos_sign > 0:
        if liq <= 0:
            return E8
        return floor_frac(Fraction((px_ticks - liq) * E8, px_ticks))
    return floor_frac(Fraction((liq - px_ticks) * E8, px_ticks))


def simulate(pack_dir: Path, actions_path: Path, n_bars: int):
    bars = load(pack_dir / "bars_1h.jsonl")
    marks = load(pack_dir / "mark_1h.jsonl")
    idx = load(pack_dir / "index_1h.jsonl")
    funding = {r["ts"]: r["rate_1e8"] for r in load(pack_dir / "funding.jsonl")}
    actions = {r["turn"]: r for r in load(actions_path)}

    pos = 0                 # signed base qty, 1e-8
    entry_ticks = 0
    target_lev_1e4 = 0      # |target leverage| backing the margin allocation
    cash = START_NAV
    fees_cum = 0
    funding_cum = 0
    realized_cum = 0
    penalty_cum = 0
    turnover = 0
    ledger: list[dict] = []
    events: list[dict] = []
    counters = {"missed": 0, "invalid": 0, "blocked": 0, "liquidations": 0}
    terminal = None  # ("liquidated", final_turn)

    def unreal(mark_ticks: int) -> int:
        return (mark_ticks - entry_ticks) * pos * TICK // E8 if pos else 0

    def emit_row(k: int, liq_override=None):
        m = marks[k]
        u = unreal(m["c"])
        liq = liq_px = None
        d = mind = None
        margin_used = 0
        if pos != 0:
            liq_px = liq_price(entry_ticks, 1 if pos > 0 else -1, target_lev_1e4)
            d = dist_1e8(m["c"], liq_px, 1 if pos > 0 else -1)
            extreme = m["l"] if pos > 0 else m["h"]
            mind = dist_1e8(extreme, liq_px, 1 if pos > 0 else -1)
            margin_used = floor_frac(
                Fraction(abs(pos) * entry_ticks * TICK, E8) / Fraction(target_lev_1e4, 10000)
            )
        ledger.append({
            "schema": "golden_ledger_row/v1",
            "bar_index": k,
            "ts": bars[k]["ts"],
            "position_base_1e8": pos,
            "entry_px_ticks": entry_ticks,
            "cash_usdt_micro": cash,
            "fees_paid_usdt_micro": fees_cum,
            "funding_paid_usdt_micro": funding_cum,
            "realized_pnl_usdt_micro": realized_cum,
            "unrealized_pnl_usdt_micro": u,
            "nav_usdt_micro": cash + u,
            "margin_used_usdt_micro": margin_used,
            "liq_px_ticks": liq_px,
            "distance_to_liq_1e8": d,
            "min_intrabar_dist_to_liq_1e8": mind,
        })

    # bar 0: flat row (no fills possible at the window start)
    emit_row(0)

    last_turn_done = -1
    for turn in range(0, n_bars - 1):
        if terminal:
            break
        fill_bar = turn + 1          # decision at close of bar `turn` fills at open of bar turn+1
        ts_decision = bars[turn]["available_at"]  # = close ts of bar `turn` = open instant of fill bar

        # ---- 1) funding settles first on the position carried into the instant
        if ts_decision in funding:
            rate = funding[ts_decision]
            index_px = idx[fill_bar]["o"]
            amt_frac = Fraction(pos * index_px * TICK, E8) * Fraction(rate, E8)
            # agent-adverse rounding = ceil in both directions:
            # positive amount (agent pays) rounds up; negative (agent receives)
            # rounds toward zero (receives less).
            amt = ceil_frac(amt_frac)
            cash -= amt
            funding_cum += amt
            events.append({
                "schema": "golden_event/v1", "type": "FundingApplied",
                "ts": ts_decision, "bar_index": fill_bar,
                "settlement_ts": ts_decision, "rate_1e8": rate,
                "index_px_ticks": index_px, "position_base_1e8": pos,
                "amount_usdt_micro": amt,
            })

        # ---- 2) the turn's action
        act = actions.get(turn)
        if act is None:
            break
        if act["mode"] == "timeout":
            counters["missed"] += 1
            events.append({
                "schema": "golden_event/v1", "type": "ActionRejected",
                "ts": ts_decision, "turn": turn, "reason": "timeout",
                "position_unchanged": True,
            })
        else:
            body = act["body"]
            try:
                parsed = json.loads(body)
                assert isinstance(parsed, dict) and parsed.get("schema") == "action/v1"
                lev = Fraction(parsed["target"]["BTC"])
                valid = True
            except Exception:
                valid = False
            if not valid:
                counters["invalid"] += 1
                events.append({
                    "schema": "golden_event/v1", "type": "ActionRejected",
                    "ts": ts_decision, "turn": turn, "reason": "invalid_json",
                    "attempts": 2, "position_unchanged": True,
                })
            else:
                lev_1e4 = int(lev * 10000)
                verdict = "pass" if abs(lev_1e4) <= LEV_CAP else "block"
                for cid, scope in (("G1_per_market_leverage", "per_market"),
                                   ("G2_gross_leverage", "gross")):
                    events.append({
                        "schema": "golden_event/v1", "type": "RiskCheck",
                        "ts": ts_decision, "turn": turn, "constraint_id": cid,
                        "scope": scope, "input_target_lev_1e4": abs(lev_1e4),
                        "limit_lev_1e4": LEV_CAP, "verdict": verdict,
                    })
                if verdict == "block":
                    counters["blocked"] += 1
                else:
                    # ---- sizing: post-funding equity, marked at decision close
                    equity = cash + unreal(marks[turn]["c"])
                    ref_px = bars[fill_bar]["o"]  # = decision-bar trade close (continuity)
                    tgt_abs = floor_frac(Fraction(abs(lev) * equity * E8) / (ref_px * TICK))
                    tgt_abs -= tgt_abs % QTY_STEP
                    tgt = tgt_abs if lev >= 0 else -tgt_abs
                    delta = tgt - pos
                    if delta != 0:
                        side = "buy" if delta > 0 else "sell"
                        req = abs(delta)
                        cap_qty = floor_frac(Fraction(bars[fill_bar]["v_base_1e8"]) * PART_CAP)
                        cap_qty -= cap_qty % QTY_STEP
                        fill_qty = min(req, cap_qty)
                        cancelled = req - fill_qty
                        hs_ticks = ceil_div(ref_px * HALF_SPREAD, E8)
                        fill_px = ref_px + hs_ticks if side == "buy" else ref_px - hs_ticks
                        notional = fill_qty * fill_px * TICK // E8
                        fee = ceil_frac(Fraction(notional * TAKER, E8))
                        # realized pnl on any closed portion
                        signed_fill = fill_qty if side == "buy" else -fill_qty
                        realized = 0
                        new_pos = pos + signed_fill
                        if pos != 0 and (new_pos == 0 or new_pos * pos < 0 or abs(new_pos) < abs(pos)):
                            closed = min(abs(pos), abs(signed_fill)) if new_pos * pos >= 0 or new_pos == 0 else abs(pos)
                            closed_signed = closed if pos > 0 else -closed
                            realized = (fill_px - entry_ticks) * closed_signed * TICK // E8
                        cash += realized - fee
                        fees_cum += fee
                        realized_cum += realized
                        turnover += notional
                        # position/entry update
                        if new_pos == 0:
                            pos, entry_ticks, target_lev_1e4 = 0, 0, 0
                        elif pos == 0 or new_pos * pos < 0:
                            pos, entry_ticks = new_pos, fill_px
                            target_lev_1e4 = abs(lev_1e4)
                        elif abs(new_pos) > abs(pos):  # add same side: weighted entry (same px here)
                            entry_notional_ticks = entry_ticks * abs(pos) + fill_px * fill_qty
                            pos = new_pos
                            assert entry_notional_ticks % abs(pos) == 0, "entry px not integer; pin rounding"
                            entry_ticks = entry_notional_ticks // abs(pos)
                            target_lev_1e4 = abs(lev_1e4)
                        else:  # pure reduce
                            pos = new_pos
                            target_lev_1e4 = abs(lev_1e4)
                        events.append({
                            "schema": "golden_event/v1", "type": "OrderFilled",
                            "ts": ts_decision, "turn": turn, "fill_bar_index": fill_bar,
                            "side": side, "ref_open_px_ticks": ref_px,
                            "half_spread_ticks": hs_ticks, "impact_ticks": 0,
                            "fill_px_ticks": fill_px,
                            "requested_qty_base_1e8": req,
                            "filled_qty_base_1e8": fill_qty,
                            "cancelled_qty_base_1e8": cancelled,
                            "notional_usdt_micro": notional,
                            "fee_usdt_micro": fee,
                            "realized_pnl_usdt_micro": realized,
                        })
                        if cancelled:
                            events.append({
                                "schema": "golden_event/v1", "type": "OrderCancelled",
                                "ts": ts_decision, "turn": turn, "fill_bar_index": fill_bar,
                                "reason": "participation_cap",
                                "requested_qty_base_1e8": req,
                                "filled_qty_base_1e8": fill_qty,
                                "cancelled_qty_base_1e8": cancelled,
                                "cap_qty_base_1e8": cap_qty,
                                "fill_bar_volume_base_1e8": bars[fill_bar]["v_base_1e8"],
                            })
        last_turn_done = turn

        # ---- 3) intra-bar liquidation check on the fill bar (mark extremes)
        if pos != 0:
            liq = liq_price(entry_ticks, 1 if pos > 0 else -1, target_lev_1e4)
            m = marks[fill_bar]
            crossed = (pos > 0 and liq is not None and liq > 0 and m["l"] <= liq) or \
                      (pos < 0 and m["h"] >= liq)
            if crossed:
                close_px = m["l"] if pos > 0 else m["h"]  # conservative extreme (gap-through)
                qty = abs(pos)
                notional = qty * close_px * TICK // E8
                signed = qty if pos > 0 else -qty
                realized = (close_px - entry_ticks) * signed * TICK // E8
                penalty = ceil_frac(Fraction(notional * LIQ_PEN, E8))
                cash += realized - penalty
                realized_cum += realized
                penalty_cum += penalty
                turnover += notional
                counters["liquidations"] += 1
                events.append({
                    "schema": "golden_event/v1", "type": "LiquidationTriggered",
                    "ts": bars[fill_bar]["ts"], "bar_index": fill_bar,
                    "trigger": "mark_low" if pos > 0 else "mark_high",
                    "trigger_px_ticks": close_px, "liq_px_ticks": liq,
                    "close_px_ticks": close_px,
                    "position_closed_base_1e8": qty,
                    "side_closed": "long" if pos > 0 else "short",
                    "realized_price_pnl_usdt_micro": realized,
                    "penalty_usdt_micro": penalty,
                    "terminal": True,
                })
                pos, entry_ticks, target_lev_1e4 = 0, 0, 0
                terminal = ("liquidated", last_turn_done)
            else:
                # near-liquidation flag (<5% intrabar distance on mark extreme)
                extreme = m["l"] if pos > 0 else m["h"]
                mind = dist_1e8(extreme, liq, 1 if pos > 0 else -1)
                if mind < 5000000:
                    events.append({
                        "schema": "golden_event/v1", "type": "NearLiquidation",
                        "ts": bars[fill_bar]["ts"], "bar_index": fill_bar,
                        "trigger": "mark_low" if pos > 0 else "mark_high",
                        "mark_extreme_px_ticks": extreme, "liq_px_ticks": liq,
                        "min_intrabar_dist_to_liq_1e8": mind,
                        "threshold_1e8": 5000000, "crossed": False,
                    })
        emit_row(fill_bar)
        if terminal:
            break

    if terminal:
        reason, final_turn = terminal
    else:
        reason, final_turn = "completed", last_turn_done
    final_nav = ledger[-1]["nav_usdt_micro"]
    events.append({
        "schema": "golden_event/v1", "type": "EpisodeEnd",
        "ts": bars[len(ledger) - 1]["available_at"],
        "reason": reason, "final_turn": final_turn,
        "final_nav_usdt_micro": final_nav,
    })

    # ---- metrics (close-of-bar NAV series; PINNED: every reported 1e-8
    #      ratio metric uses floor of the exact rational)
    navs = [r["nav_usdt_micro"] for r in ledger]
    peak = navs[0]
    max_dd = Fraction(0)
    peak_at_max = trough = navs[0]
    trough_bar = 0
    for i, nav in enumerate(navs):
        peak = max(peak, nav)
        dd = Fraction((peak - nav) * E8, peak)
        if dd > max_dd:
            max_dd, peak_at_max, trough, trough_bar = dd, peak, nav, i
    metrics = {
        "schema": "golden_metrics/v1",
        "cost_profile": "primary",
        "final_nav_usdt_micro": final_nav,
        "net_return_1e8": floor_frac(Fraction((final_nav - START_NAV) * E8, START_NAV)),
        "max_drawdown_1e8": floor_frac(max_dd),
        "max_drawdown_peak_nav_usdt_micro": peak_at_max,
        "max_drawdown_trough_nav_usdt_micro": trough,
        "max_drawdown_trough_bar_index": trough_bar,
        "total_fees_usdt_micro": fees_cum,
        "total_funding_usdt_micro": funding_cum,
        "total_liq_penalty_usdt_micro": penalty_cum,
        "turnover_notional_usdt_micro": turnover,
        "turnover_ratio_1e8": floor_frac(Fraction(turnover * E8, START_NAV)),
        "kill_switch_triggered": False,
        "liquidations": counters["liquidations"],
        "missed_decisions": counters["missed"],
        "invalid_actions": counters["invalid"],
        "blocked_actions": counters["blocked"],
        "episode_end_reason": reason,
    }
    return ledger, events, metrics


def write_canonical(path: Path, rows):
    if isinstance(rows, list):
        data = b"".join(canonical_bytes(r) + b"\n" for r in rows)
    else:
        data = canonical_bytes(rows) + b"\n"
    path.write_bytes(data)


def main() -> None:
    main_out = simulate(ROOT / "pack", ROOT / "actions.jsonl", 14)
    var_out = simulate(ROOT / "variant-liquidation/pack",
                       ROOT / "variant-liquidation/actions.jsonl", 14)

    # C0.3c note: this script's golden_* shapes are the C0.3b working format,
    # superseded by the published-v1 expected files written by
    # emit_expected_v1.py. To keep the reconciliation reproducible WITHOUT
    # clobbering the v1 oracle in expected/, output now goes to
    # derivations/c03b-shapes/ (same bytes as the §"Reconciled truth" hashes).
    for name, (ledger, events, metrics) in (("main", main_out),
                                            ("variant-liquidation", var_out)):
        d = ROOT / "derivations" / "c03b-shapes" / name
        d.mkdir(parents=True, exist_ok=True)
        write_canonical(d / "ledger.jsonl", ledger)
        write_canonical(d / "events.jsonl", events)
        write_canonical(d / "metrics.json", metrics)

    # ---------- discrepancy scan: my values vs calc-A and calc-B ----------
    calc_a = json.loads((ROOT / "derivations/calc-A.json").read_text())
    calc_b = json.loads((ROOT / "derivations/calc-B.json").read_text())

    def cmp_ledger(mine, theirs, who, tag, posfield):
        diffs = []
        for r_m, r_t in zip(mine, theirs):
            for k, v in r_m.items():
                if k == "schema":
                    continue
                tk = posfield if k == "position_base_1e8" else k
                if tk not in r_t:
                    diffs.append((tag, r_m["bar_index"], k, "MISSING", v))
                    continue
                if r_t[tk] != v:
                    diffs.append((tag, r_m["bar_index"], k, r_t[tk], v))
        for tag2, bar, k, theirs_v, mine_v in diffs:
            print(f"  {who} {tag2} bar{bar} {k}: {who}={theirs_v} reconciled={mine_v}")
        return diffs

    print("== ledger discrepancies vs reconciled truth ==")
    da = cmp_ledger(main_out[0], calc_a["main_ledger"], "A", "main", "position_contracts")
    da += cmp_ledger(var_out[0], calc_a["variant_ledger"], "A", "variant", "position_contracts")
    db = cmp_ledger(main_out[0], calc_b["main_ledger"], "B", "main", "position_contracts_1e8")
    db += cmp_ledger(var_out[0], calc_b["variant_ledger"], "B", "variant", "position_contracts_1e8")
    print(f"A ledger diffs: {len(da)}  B ledger diffs: {len(db)}")

    print("== A-vs-B ledger discrepancy list (field level) ==")
    ab = []
    for tag, la, lb in (("main", calc_a["main_ledger"], calc_b["main_ledger"]),
                        ("variant", calc_a["variant_ledger"], calc_b["variant_ledger"])):
        for ra, rb in zip(la, lb):
            keys = (set(ra) | set(rb)) - {"near_liq", "note", "terminal",
                                          "liq_penalty_cumulative_micro",
                                          "position_contracts", "position_contracts_1e8"}
            if ra.get("position_contracts") != rb.get("position_contracts_1e8"):
                ab.append((tag, ra["bar_index"], "position", ra.get("position_contracts"), rb.get("position_contracts_1e8")))
            for k in sorted(keys):
                if ra.get(k) != rb.get(k):
                    ab.append((tag, ra["bar_index"], k, ra.get(k), rb.get(k)))
    for row in ab:
        print("  ", row)
    print(f"A-vs-B ledger field discrepancies: {len(ab)}")

    print("== metrics ==")
    print("main:", json.dumps(main_out[2]))
    print("variant:", json.dumps(var_out[2]))

    # ---------- invariant verification over the WRITTEN files ----------
    print("== invariants over written expected files ==")
    ok_all = True
    for name in ("main", "variant-liquidation"):
        d = ROOT / "derivations" / "c03b-shapes" / name
        rows = [json.loads(x) for x in (d / "ledger.jsonl").read_text().splitlines()]
        evs = [json.loads(x) for x in (d / "events.jsonl").read_text().splitlines()]
        met = json.loads((d / "metrics.json").read_text())
        ok = True
        for i in range(1, len(rows)):
            p, c = rows[i - 1], rows[i]
            evs_bar = [e for e in evs if e.get("ts") == p["ts"] + 3600000 or
                       (e.get("bar_index") == c["bar_index"] and e["type"] == "LiquidationTriggered")]
            fees = sum(e.get("fee_usdt_micro", 0) for e in evs_bar if e["type"] == "OrderFilled")
            fund = sum(e.get("amount_usdt_micro", 0) for e in evs_bar if e["type"] == "FundingApplied")
            real = sum(e.get("realized_pnl_usdt_micro", 0) for e in evs_bar if e["type"] == "OrderFilled")
            pen = sum(e.get("penalty_usdt_micro", 0) for e in evs_bar if e["type"] == "LiquidationTriggered")
            real += sum(e.get("realized_price_pnl_usdt_micro", 0) for e in evs_bar
                        if e["type"] == "LiquidationTriggered")
            # INV-1 cash conservation
            if c["cash_usdt_micro"] - p["cash_usdt_micro"] != real - fees - fund - pen:
                print(f"  FAIL INV-1 {name} bar {c['bar_index']}")
                ok = False
            # INV-2 NAV attribution: dNAV == d(unrealized+realized as price pnl) - fees - funding - penalty
            price_pnl = (c["unrealized_pnl_usdt_micro"] - p["unrealized_pnl_usdt_micro"]) + real
            dnav = c["nav_usdt_micro"] - p["nav_usdt_micro"]
            if dnav != price_pnl - fees - fund - pen:
                print(f"  FAIL INV-2 {name} bar {c['bar_index']}")
                ok = False
            # INV-3 NAV identity
            if c["nav_usdt_micro"] != c["cash_usdt_micro"] + c["unrealized_pnl_usdt_micro"]:
                print(f"  FAIL INV-3 {name} bar {c['bar_index']}")
                ok = False
        # INV-4 metrics recomputation from ledger
        navs = [r["nav_usdt_micro"] for r in rows]
        assert met["final_nav_usdt_micro"] == navs[-1]
        nr = ((navs[-1] - START_NAV) * E8) // START_NAV
        assert met["net_return_1e8"] == nr, (met["net_return_1e8"], nr)
        peak = navs[0]; mdd = Fraction(0)
        for nav in navs:
            peak = max(peak, nav)
            mdd = max(mdd, Fraction((peak - nav) * E8, peak))
        assert met["max_drawdown_1e8"] == mdd.numerator // mdd.denominator
        fees_l = rows[-1]["fees_paid_usdt_micro"]
        assert met["total_fees_usdt_micro"] == fees_l
        assert met["total_funding_usdt_micro"] == rows[-1]["funding_paid_usdt_micro"]
        print(f"  {name}: INV-1 cash conservation OK, INV-2 NAV attribution OK, "
              f"INV-3 NAV=cash+unrealized OK, INV-4 metrics-from-ledger OK "
              f"({len(rows)} rows)" if ok else f"  {name}: FAILURES ABOVE")
        ok_all = ok_all and ok
    print("ALL INVARIANTS:", "PASS" if ok_all else "FAIL")


if __name__ == "__main__":
    main()
