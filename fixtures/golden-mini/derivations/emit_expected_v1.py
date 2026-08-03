"""C0.3c (M0 audit round 1): re-express the reconciled golden-mini expected
outputs in the FINAL published schemas — event/v1, ledger_row/v1, metrics/v1 —
so the MATH-1 "byte-identical vs hand-calc" oracle diffs directly against
engine output, and add the MATH-2 delta-NAV attribution terms to every ledger
row (finding SCH-1/MATH-1 and finding MATH-2).

Economics are IDENTICAL to the reconciled C0.3b rules (reconcile_check.py /
reconciliation.md); this script re-runs the same pinned arithmetic, asserts
equality with the reconciled headline anchors, adds the stress_2x cost-profile
re-simulation (2x multiplier on spread+fee+impact, same action trace), maps
everything into the v1 shapes, VALIDATES every emitted line against the
schemas, and re-checks the MATH-2 identity on the written bytes.

All arithmetic exact: ints + Fraction. No floats anywhere.

C0.3d (M0 audit round 2): the synthetic venue now runs a 4h funding interval
(stamps 08/12/16/20 in-window; reconciliation.md section 8 has the arithmetic),
ActionParsed payloads carry the canonical `intent_kind` discriminator (IC-3
seam), and the emitted events.jsonl is the DEFINED economic projection of the
IC-4 stream (scenario.md "Event-stream scope": asserted types only, seq
renumbered contiguously; ObservationEmitted/AgentResponded/MarginUpdate are
out of fixture scope — no observation/raw artifacts exist to hash).
"""
from __future__ import annotations

import json
import math
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, "/Users/leonliu/Desktop/TradeEvolve")
from spec.canonical import canonical_bytes, sha256_prefixed  # noqa: E402

from jsonschema import Draft202012Validator  # noqa: E402

ROOT = Path("/Users/leonliu/Desktop/TradeEvolve/fixtures/golden-mini")
SCHEMAS = Path("/Users/leonliu/Desktop/TradeEvolve/spec/schemas")

TICK = 10000            # micro per tick
QTY_STEP = 100000       # 0.001 BTC in 1e-8
TAKER = 45000           # 1e-8, primary
HALF_SPREAD = 50000     # 1e-8, primary
PART_CAP = Fraction(10000000, 10**8)  # 10%
MM = 16666667           # 1e-8
LIQ_PEN = 1000000       # 1e-8 (1%)
LEV_CAP = 30000         # 1e-4 (gross and per-market)
DD_KS = 20000000        # 1e-8 (20%) drawdown kill switch limit
START_NAV = 10**10
E8 = 10**8
NEAR_LIQ_THRESHOLD = 5000000
RESPONSE_DEADLINE_MS = 120000

# Placeholder run identities for the fixture (documented in scenario.md; a
# fixture has no harness-minted run id, but metrics/v1 requires one).
RUN_IDS = {"main": "run_00000000000000a1", "variant-liquidation": "run_00000000000000b1"}

# Deterministic diagnostic templates pinned by this fixture (IC-4 detail fields).
DETAIL_TIMEOUT = f"no response bytes within response_deadline_ms={RESPONSE_DEADLINE_MS}"
DETAIL_INVALID_JSON = "response is not a single valid JSON document"


def ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def floor_frac(f: Fraction) -> int:
    return f.numerator // f.denominator


def ceil_frac(f: Fraction) -> int:
    return -((-f.numerator) // f.denominator)


def load(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def liq_price(entry_ticks: int, pos_sign: int, lev_1e4: int) -> int:
    """Pinned (reconciliation D1): maintenance-crossing formula on entry px +
    target leverage. long: ceil(E*(L-1)/L / (1-mm)); short: floor(E*(L+1)/L /
    (1+mm)). Long with lev<=1x: clamp to 0 (unreachable)."""
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


def maintenance_micro(pos_abs: int, mark_ticks: int) -> int:
    """Maintenance margin required at mark notional (agent-adverse ceil):
    ceil(|pos| * mark * TICK * MM / 1e16)."""
    if pos_abs == 0:
        return 0
    return ceil_frac(Fraction(pos_abs * mark_ticks * TICK * MM, E8 * E8))


def simulate(pack_dir: Path, actions_path: Path, cost_mult: int):
    """One episode under one cost profile (cost_mult 1 = primary, 2 = stress_2x).
    Returns (bars_state, econ_events, per_bar, counters, totals)."""
    taker = TAKER * cost_mult
    half_spread = HALF_SPREAD * cost_mult

    bars = load(pack_dir / "bars_1h.jsonl")
    marks = load(pack_dir / "mark_1h.jsonl")
    idx = load(pack_dir / "index_1h.jsonl")
    funding = {r["ts"]: r["rate_1e8"] for r in load(pack_dir / "funding.jsonl")}
    actions = {r["turn"]: r for r in load(actions_path)}
    n_bars = len(bars)

    pos = 0
    entry_ticks = 0
    target_lev_1e4 = 0
    cash = START_NAV
    fees_cum = 0
    funding_cum = 0        # positive = paid
    realized_price_cum = 0  # price PnL realized by fills / forced close
    penalty_cum = 0
    turnover = 0
    fill_cost = 0          # spread+impact cost vs reference opens, positive
    per_bar: list[dict] = []   # snapshot per emitted ledger bar
    econ: list[dict] = []      # economic events (pre-envelope), primary use only
    counters = {"missed": 0, "invalid": 0, "blocked": 0, "liquidations": 0,
                "gate_blocks": 0, "turns": 0}
    terminal = None

    def unreal(mark_ticks: int) -> int:
        return (mark_ticks - entry_ticks) * pos * TICK // E8 if pos else 0

    def snapshot(k: int):
        m = marks[k]
        u = unreal(m["c"])
        liq_px = d = mind = None
        margin_used = 0
        if pos != 0:
            liq_px = liq_price(entry_ticks, 1 if pos > 0 else -1, target_lev_1e4)
            d = dist_1e8(m["c"], liq_px, 1 if pos > 0 else -1)
            extreme = m["l"] if pos > 0 else m["h"]
            mind = dist_1e8(extreme, liq_px, 1 if pos > 0 else -1)
            margin_used = floor_frac(
                Fraction(abs(pos) * entry_ticks * TICK, E8) / Fraction(target_lev_1e4, 10000)
            )
        per_bar.append({
            "bar_index": k,
            "ts": bars[k]["ts"],          # NOTE: ledger ts uses bar close below
            "close_ts": bars[k]["available_at"],
            "pos": pos, "entry": entry_ticks if pos else None,
            "mark_close": m["c"],
            "upnl": u, "cash": cash, "nav": cash + u,
            "fees_cum": fees_cum, "funding_cum": funding_cum,
            "realized_price_cum": realized_price_cum, "penalty_cum": penalty_cum,
            "margin": margin_used,
            "maintenance": maintenance_micro(abs(pos), m["c"]),
            "liq_px": liq_px, "dist": d, "min_intrabar": mind,
        })

    snapshot(0)
    last_turn_done = -1
    for turn in range(0, n_bars - 1):
        if terminal:
            break
        fill_bar = turn + 1
        ts_decision = bars[turn]["available_at"]

        # ---- 1) the turn's action: parse outcome first (meaning extraction)
        act = actions.get(turn)
        if act is None:
            break
        counters["turns"] += 1
        parsed_lev = None
        if act["mode"] == "timeout":
            counters["missed"] += 1
            econ.append({"type": "ActionRejected", "ts": ts_decision, "turn": turn,
                         "bar_index": turn,
                         "payload": {"reason": "timeout", "detail": DETAIL_TIMEOUT,
                                     "validator_error": None, "attempts": 1}})
        else:
            body = act["body"]
            try:
                p = json.loads(body)
                assert isinstance(p, dict) and p.get("schema") == "action/v1"
                parsed_lev = Fraction(p["target"]["BTC"])
            except Exception:
                parsed_lev = None
                counters["invalid"] += 1
                econ.append({"type": "ActionRejected", "ts": ts_decision, "turn": turn,
                             "bar_index": turn,
                             "payload": {"reason": "invalid_json",
                                         "detail": "attempt 2: " + DETAIL_INVALID_JSON,
                                         "validator_error": "invalid_json: " + DETAIL_INVALID_JSON,
                                         "attempts": 2}})
            else:
                lev_1e4 = int(parsed_lev * 10000)
                econ.append({"type": "ActionParsed", "ts": ts_decision, "turn": turn,
                             "bar_index": turn,
                             "payload": {"intent_kind": "leverage_target",
                                         "target_lev_1e4": {"BTC": lev_1e4},
                                         "max_slippage_bps": None, "from_attempt": 1}})

        # ---- 2) funding settles on the position carried INTO the instant
        #         (pinned same-instant order: funding -> sizing/gates -> fill)
        if ts_decision in funding:
            rate = funding[ts_decision]
            index_px = idx[fill_bar]["o"]
            amt = ceil_frac(Fraction(pos * index_px * TICK, E8) * Fraction(rate, E8))
            cash -= amt
            funding_cum += amt
            econ.append({"type": "FundingApplied", "ts": ts_decision, "turn": turn,
                         "bar_index": fill_bar,
                         "payload": {"market": "BTC", "settlement_ts": ts_decision,
                                     "rate_1e8": rate, "index_px_ticks": index_px,
                                     "position_qty_base_1e8": pos,
                                     "amount_micro": -amt}})

        # ---- 3) gates + fill for parsed actions
        if parsed_lev is not None:
            lev_1e4 = int(parsed_lev * 10000)
            # peak-to-close drawdown of the ledger NAV series through bar `turn`
            navs_so_far = [row["nav"] for row in per_bar]
            peak = max(navs_so_far)
            dd_now = floor_frac(Fraction((peak - navs_so_far[-1]) * E8, peak))
            lev_verdict = "pass" if abs(lev_1e4) <= LEV_CAP else "block"
            for cid, ctype, scope in (("lev-BTC", "leverage_cap_market", "BTC"),
                                      ("lev-gross", "leverage_cap_gross", "account")):
                econ.append({"type": "RiskCheck", "ts": ts_decision, "turn": turn,
                             "bar_index": turn,
                             "payload": {"constraint_id": cid, "constraint_type": ctype,
                                         "scope": scope, "observed": abs(lev_1e4),
                                         "limit": LEV_CAP, "unit": "lev_1e4",
                                         "verdict": lev_verdict}})
                if lev_verdict == "block":
                    counters["gate_blocks"] += 1
            econ.append({"type": "RiskCheck", "ts": ts_decision, "turn": turn,
                         "bar_index": turn,
                         "payload": {"constraint_id": "drawdown-ks",
                                     "constraint_type": "drawdown_kill_switch",
                                     "scope": "account", "observed": dd_now,
                                     "limit": DD_KS, "unit": "1e8", "verdict": "pass"}})
            if lev_verdict == "block":
                counters["blocked"] += 1
            else:
                equity = cash + unreal(marks[turn]["c"])
                ref_px = bars[fill_bar]["o"]
                tgt_abs = floor_frac(Fraction(abs(parsed_lev) * equity * E8) / (ref_px * TICK))
                tgt_abs -= tgt_abs % QTY_STEP
                tgt = tgt_abs if parsed_lev >= 0 else -tgt_abs
                delta = tgt - pos
                if delta != 0:
                    side = "buy" if delta > 0 else "sell"
                    req = abs(delta)
                    cap_qty = floor_frac(Fraction(bars[fill_bar]["v_base_1e8"]) * PART_CAP)
                    cap_qty -= cap_qty % QTY_STEP
                    fill_qty = min(req, cap_qty)
                    cancelled = req - fill_qty
                    hs_ticks = ceil_div(ref_px * half_spread, E8)
                    fill_px = ref_px + hs_ticks if side == "buy" else ref_px - hs_ticks
                    notional = fill_qty * fill_px * TICK // E8
                    notional_ref = fill_qty * ref_px * TICK // E8
                    fee = ceil_frac(Fraction(notional * taker, E8))
                    slip = floor_frac(Fraction((fill_px - ref_px) * E8, ref_px))
                    signed_fill = fill_qty if side == "buy" else -fill_qty
                    realized = 0
                    new_pos = pos + signed_fill
                    if pos != 0 and (new_pos == 0 or new_pos * pos < 0 or abs(new_pos) < abs(pos)):
                        closed = min(abs(pos), abs(signed_fill)) if new_pos * pos >= 0 or new_pos == 0 else abs(pos)
                        closed_signed = closed if pos > 0 else -closed
                        realized = (fill_px - entry_ticks) * closed_signed * TICK // E8
                    cash += realized - fee
                    fees_cum += fee
                    realized_price_cum += realized
                    turnover += notional
                    fill_cost += abs(notional - notional_ref)
                    if new_pos == 0:
                        pos, entry_ticks, target_lev_1e4 = 0, 0, 0
                    elif pos == 0 or new_pos * pos < 0:
                        pos, entry_ticks = new_pos, fill_px
                        target_lev_1e4 = abs(lev_1e4)
                    elif abs(new_pos) > abs(pos):
                        entry_notional_ticks = entry_ticks * abs(pos) + fill_px * fill_qty
                        pos = new_pos
                        assert entry_notional_ticks % abs(pos) == 0
                        entry_ticks = entry_notional_ticks // abs(pos)
                        target_lev_1e4 = abs(lev_1e4)
                    else:
                        pos = new_pos
                        target_lev_1e4 = abs(lev_1e4)
                    econ.append({"type": "OrderFilled", "ts": ts_decision, "turn": turn,
                                 "bar_index": fill_bar,
                                 "payload": {"market": "BTC", "side": side,
                                             "requested_qty_base_1e8": req,
                                             "qty_base_1e8": fill_qty,
                                             "ref_open_px_ticks": ref_px,
                                             "half_spread_ticks": hs_ticks,
                                             "impact_ticks": 0,
                                             "fill_px_ticks": fill_px,
                                             "notional_micro": notional,
                                             "fee_micro": fee,
                                             "slippage_1e8": slip,
                                             "cost_profile": "primary"}})
                    if cancelled:
                        econ.append({"type": "OrderCancelled", "ts": ts_decision, "turn": turn,
                                     "bar_index": fill_bar,
                                     "payload": {"market": "BTC",
                                                 "reason": "participation_cap",
                                                 "requested_qty_base_1e8": req,
                                                 "cancelled_qty_base_1e8": cancelled,
                                                 "detail": (
                                                     f"participation_cap: requested_qty {req} > cap_qty {cap_qty} "
                                                     f"(participation_cap_1e8 10000000 x fill-bar volume "
                                                     f"{bars[fill_bar]['v_base_1e8']}, floored to qty_step {QTY_STEP})")}})
        last_turn_done = turn

        # ---- 4) intra-bar liquidation / near-liquidation on the holding bar
        if pos != 0:
            liq = liq_price(entry_ticks, 1 if pos > 0 else -1, target_lev_1e4)
            m = marks[fill_bar]
            crossed = (pos > 0 and liq > 0 and m["l"] <= liq) or (pos < 0 and m["h"] >= liq)
            if crossed:
                close_px = m["l"] if pos > 0 else m["h"]
                qty = abs(pos)
                notional = qty * close_px * TICK // E8
                signed = qty if pos > 0 else -qty
                realized = (close_px - entry_ticks) * signed * TICK // E8
                penalty = ceil_frac(Fraction(notional * LIQ_PEN, E8))
                cash += realized - penalty
                realized_price_cum += realized
                penalty_cum += penalty
                turnover += notional
                counters["liquidations"] += 1
                econ.append({"type": "LiquidationTriggered", "ts": bars[fill_bar]["ts"],
                             "turn": turn, "bar_index": fill_bar,
                             "payload": {"market": "BTC",
                                         "trigger": "mark_low" if pos > 0 else "mark_high",
                                         "trigger_px_ticks": close_px,
                                         "liq_px_ticks": liq,
                                         "close_px_ticks": close_px,
                                         "position_qty_base_1e8": pos,
                                         "penalty_micro": penalty,
                                         "loss_micro": realized - penalty}})
                pos, entry_ticks, target_lev_1e4 = 0, 0, 0
                terminal = ("liquidated", last_turn_done)
            else:
                extreme = m["l"] if pos > 0 else m["h"]
                mind = dist_1e8(extreme, liq, 1 if pos > 0 else -1)
                if mind < NEAR_LIQ_THRESHOLD:
                    econ.append({"type": "NearLiquidation", "ts": bars[fill_bar]["ts"],
                                 "turn": turn, "bar_index": fill_bar,
                                 "payload": {"market": "BTC",
                                             "trigger": "mark_low" if pos > 0 else "mark_high",
                                             "mark_extreme_px_ticks": extreme,
                                             "liq_px_ticks": liq,
                                             "min_intrabar_dist_to_liq_1e8": mind,
                                             "threshold_1e8": NEAR_LIQ_THRESHOLD}})
        snapshot(fill_bar)
        if terminal:
            break

    reason, final_turn = terminal if terminal else ("completed", last_turn_done)
    totals = {"reason": reason, "final_turn": final_turn,
              "final_nav": per_bar[-1]["nav"], "turnover": turnover,
              "fill_cost": fill_cost, "fees_cum": fees_cum,
              "funding_cum": funding_cum, "penalty_cum": penalty_cum}
    return per_bar, econ, counters, totals


# ---------------------------------------------------------------------------
# metric estimators (pinned at C0.3c; exact integer/Fraction arithmetic)
# ---------------------------------------------------------------------------

def bar_returns(navs: list[int]) -> list[Fraction]:
    return [Fraction(navs[i] - navs[i - 1], navs[i - 1]) for i in range(1, len(navs))]


def max_drawdown_1e8(navs: list[int]) -> int:
    peak, mdd = navs[0], Fraction(0)
    for nav in navs:
        peak = max(peak, nav)
        mdd = max(mdd, Fraction((peak - nav) * E8, peak))
    return floor_frac(mdd)


def floor_a_over_sqrt_b(a: Fraction, b: Fraction) -> int:
    """floor(a / sqrt(b)) exactly, b > 0."""
    q = (a * a) / b   # (a/sqrt(b))^2 as exact rational
    n = math.isqrt(floor_frac(q))
    if a >= 0:
        return n
    # a < 0: floor = -ceil(|a|/sqrt(b))
    exact = (q.denominator == 1 and n * n == q.numerator)
    return -(n if exact else n + 1)


def sortino_1e8(returns: list[Fraction]) -> int:
    mean_r = sum(returns, Fraction(0)) / len(returns)
    downside = [min(r, Fraction(0)) for r in returns]
    msd = sum((d * d for d in downside), Fraction(0)) / len(returns)
    assert msd > 0, "sortino undefined (no downside) - not the case in this fixture"
    return floor_a_over_sqrt_b(mean_r * E8, msd)


def cvar5_1e8(returns: list[Fraction]) -> int:
    k = ceil_div(len(returns), 20)  # ceil(0.05 * n)
    worst = sorted(returns)[:k]
    return floor_frac(sum(worst, Fraction(0)) * E8 / k)


def nearest_rank(sorted_vals: list[int], p_num: int, p_den: int) -> int:
    n = len(sorted_vals)
    rank = max(1, ceil_div(p_num * n, p_den))
    return sorted_vals[rank - 1]


# ---------------------------------------------------------------------------
# shape mappers
# ---------------------------------------------------------------------------

def ledger_rows(per_bar: list[dict], profile: str) -> list[dict]:
    rows = []
    prev = None
    for row in per_bar:
        if prev is None:
            d_nav = d_price = d_fund = d_fees = d_pen = 0
        else:
            d_nav = row["nav"] - prev["nav"]
            d_fees = -(row["fees_cum"] - prev["fees_cum"])
            d_fund = -(row["funding_cum"] - prev["funding_cum"])
            d_pen = -(row["penalty_cum"] - prev["penalty_cum"])
            d_price = (row["upnl"] - prev["upnl"]) + (row["realized_price_cum"] - prev["realized_price_cum"])
        assert d_nav == d_price + d_fund + d_fees + d_pen, ("MATH-2", profile, row["bar_index"])
        realized_pnl = (row["realized_price_cum"] - row["fees_cum"]
                        - row["funding_cum"] - row["penalty_cum"])
        rows.append({
            "ts": row["close_ts"],
            "bar_index": row["bar_index"],
            "turn": None if row["bar_index"] == 0 else row["bar_index"] - 1,
            "profile": profile,
            "nav_micro": row["nav"],
            "cash_micro": row["cash"],
            "realized_pnl_micro": realized_pnl,
            "d_nav_micro": d_nav,
            "d_price_pnl_micro": d_price,
            "d_funding_micro": d_fund,
            "d_fees_micro": d_fees,
            "d_liq_penalty_micro": d_pen,
            "positions": {
                "BTC": {
                    "qty_base_1e8": row["pos"],
                    "entry_px_ticks": row["entry"],
                    "mark_px_ticks": row["mark_close"],
                    "upnl_micro": row["upnl"],
                    "margin_micro": row["margin"],
                    "maintenance_margin_micro": row["maintenance"],
                    "liq_px_ticks": row["liq_px"],
                    "dist_to_liq_1e8": row["dist"],
                }
            },
        })
        prev = row
    return rows


def profile_metrics(per_bar: list[dict], totals: dict, ref: str) -> dict:
    navs = [r["nav"] for r in per_bar]
    rets = bar_returns(navs)
    in_pos = [r for r in per_bar if r["pos"] != 0]
    dists = sorted(r["dist"] for r in in_pos)
    minds = [r["min_intrabar"] for r in in_pos]
    return {
        "net_return_1e8": floor_frac(Fraction((totals["final_nav"] - START_NAV) * E8, START_NAV)),
        "max_drawdown_1e8": max_drawdown_1e8(navs),
        "sortino_1e8": sortino_1e8(rets),
        "cvar5_1e8": cvar5_1e8(rets),
        "funding_paid_micro": -totals["funding_cum"],
        "fees_paid_micro": -totals["fees_cum"],
        "fill_cost_micro": -totals["fill_cost"],
        "turnover_1e8": floor_frac(Fraction(totals["turnover"] * E8, START_NAV)),
        "dist_to_liq_min_1e8": min(minds) if minds else None,
        "dist_to_liq_p05_1e8": nearest_rank(dists, 1, 20) if dists else None,
        "dist_to_liq_p25_1e8": nearest_rank(dists, 1, 4) if dists else None,
        "dist_to_liq_median_1e8": nearest_rank(dists, 1, 2) if dists else None,
        "equity_curve_ref": ref,
    }


def build_metrics(run_id: str, prim, stress) -> dict:
    per_bar_p, _, counters_p, totals_p = prim
    per_bar_s, _, counters_s, totals_s = stress
    inv = {
        "bars": len(per_bar_p),
        "turns": counters_p["turns"],
        "invalid_actions": counters_p["invalid"],
        "missed_decisions": counters_p["missed"],
        "gate_blocks": counters_p["gate_blocks"],
        "post_kill_switch_attempts": 0,
        "egress_blocked_count": 0,
        "liquidated": counters_p["liquidations"] > 0,
        "kill_switch_fired": False,
        "survival_verdict": "liquidated" if counters_p["liquidations"] else "survived",
    }
    inv_s = {
        "bars": len(per_bar_s), "turns": counters_s["turns"],
        "invalid_actions": counters_s["invalid"], "missed_decisions": counters_s["missed"],
        "gate_blocks": counters_s["gate_blocks"], "post_kill_switch_attempts": 0,
        "egress_blocked_count": 0, "liquidated": counters_s["liquidations"] > 0,
        "kill_switch_fired": False,
        "survival_verdict": "liquidated" if counters_s["liquidations"] else "survived",
    }
    assert inv == inv_s, "profile_invariant differs between cost profiles"
    return {
        "schema": "metrics/v1",
        "run_id": run_id,
        "claim_label": "survival-stress",
        "profiles": {
            "primary": profile_metrics(per_bar_p, totals_p, "ledger.jsonl"),
            "stress_2x": profile_metrics(per_bar_s, totals_s, "ledger_stress_2x.jsonl"),
        },
        "profile_invariant": inv,
    }


def envelope_events(econ: list[dict], end_ts: int, reason: str, final_turn: int,
                    final_nav: int, metrics_sha: str) -> list[dict]:
    events = []
    for e in econ:
        events.append({
            "schema": "event/v1", "seq": len(events), "ts": e["ts"],
            "turn": e["turn"], "bar_index": e["bar_index"],
            "source": "engine", "type": e["type"], "payload": e["payload"],
        })
    events.append({
        "schema": "event/v1", "seq": len(events), "ts": end_ts,
        "turn": None, "bar_index": None, "source": "engine", "type": "EpisodeEnd",
        "payload": {"reason": reason, "final_turn": final_turn,
                    "final_nav_micro": final_nav, "metrics_sha256": metrics_sha},
    })
    return events


def write_canonical(path: Path, rows) -> bytes:
    if isinstance(rows, list):
        data = b"".join(canonical_bytes(r) + b"\n" for r in rows)
    else:
        data = canonical_bytes(rows) + b"\n"
    path.write_bytes(data)
    return data


def main() -> None:
    event_schema = json.loads((SCHEMAS / "event.v1.schema.json").read_text())
    ledger_schema = json.loads((SCHEMAS / "ledger_row.v1.schema.json").read_text())
    metrics_schema = json.loads((SCHEMAS / "metrics.v1.schema.json").read_text())
    v_event = Draft202012Validator(event_schema)
    v_ledger = Draft202012Validator(ledger_schema)
    v_metrics = Draft202012Validator(metrics_schema)

    episodes = {
        "main": (ROOT / "pack", ROOT / "actions.jsonl"),
        "variant-liquidation": (ROOT / "variant-liquidation/pack",
                                ROOT / "variant-liquidation/actions.jsonl"),
    }
    # C0.3d anchors (reconciliation.md section 8): the C0.3b truth adjusted for
    # the two ADDED 4h-interval funding stamps (12:00 normal-rate on the held
    # 2.3 BTC long: -2,863,500; 20:00 negative-rate on the held 1.4 BTC short:
    # -1,620,500). Every fill is byte-identical to C0.3b (step-floor sizing
    # absorbs the equity shifts), so fees/turnover/liq prices are unchanged and
    # the NAV deltas are exactly the funding amounts.
    anchors = {
        "main": {"final_nav": 9029065290, "fees": 31975200, "funding": 30991730,
                 "penalty": 0, "turnover": 71055996220, "net_return": -9709348,
                 "max_dd": 11755971, "missed": 6, "invalid": 1},
        "variant-liquidation": {"final_nav": 2003481325, "fees": 10355175,
                                "funding": 2863500, "penalty": 151800000,
                                "turnover": 38191500000, "net_return": -79965187,
                                "max_dd": 79967181, "missed": 2, "invalid": 1},
    }

    for name, (pack, acts) in episodes.items():
        prim = simulate(pack, acts, 1)
        stress = simulate(pack, acts, 2)
        per_bar_p, econ_p, counters_p, totals_p = prim

        # -- regression anchors against the reconciled truth
        a = anchors[name]
        navs = [r["nav"] for r in per_bar_p]
        assert totals_p["final_nav"] == a["final_nav"], (name, totals_p["final_nav"])
        assert totals_p["fees_cum"] == a["fees"]
        assert totals_p["funding_cum"] == a["funding"]
        assert totals_p["penalty_cum"] == a["penalty"]
        assert totals_p["turnover"] == a["turnover"]
        assert floor_frac(Fraction((totals_p["final_nav"] - START_NAV) * E8, START_NAV)) == a["net_return"]
        assert max_drawdown_1e8(navs) == a["max_dd"]
        assert counters_p["missed"] == a["missed"] and counters_p["invalid"] == a["invalid"]
        if name == "main":
            liq_pxs = {r["liq_px"] for r in per_bar_p if r["liq_px"] is not None}
            assert liq_pxs == {600301, 720361, 0, 1347897}, liq_pxs
            near = [e for e in econ_p if e["type"] == "NearLiquidation"]
            assert len(near) == 1 and near[0]["payload"]["min_intrabar_dist_to_liq_1e8"] == 3307248
            # C0.3d funding coverage: zero-position edge (08:00), NORMAL rate on a
            # held long (12:00, uncapped magnitude pinned), +cap on a held long
            # (16:00), NEGATIVE rate paid by a held short (20:00).
            fund = [e["payload"] for e in econ_p if e["type"] == "FundingApplied"]
            assert [(f["rate_1e8"], f["position_qty_base_1e8"], f["amount_micro"])
                    for f in fund] == [
                (10000, 0, 0),
                (12500, 230000000, -2863500),
                (300000, 93700000, -26507730),
                (-12500, -140000000, -1620500),
            ], fund
        else:
            liq = [e for e in econ_p if e["type"] == "LiquidationTriggered"]
            assert len(liq) == 1
            assert liq[0]["payload"]["penalty_micro"] == 151800000
            assert liq[0]["payload"]["loss_micro"] == -7831500000 - 151800000
            assert liq[0]["payload"]["liq_px_ticks"] == 720361

        # -- metrics (needed first: EpisodeEnd carries metrics_sha256)
        metrics = build_metrics(RUN_IDS[name], prim, stress)
        errs = list(v_metrics.iter_errors(metrics))
        assert not errs, (name, "metrics", [e.message for e in errs[:5]])
        out = ROOT / "expected" / name
        metrics_bytes = write_canonical(out / "metrics.json", metrics)
        metrics_sha = sha256_prefixed(metrics_bytes)

        # -- events (primary profile only, economic subset, seq renumbered)
        end_ts = per_bar_p[-1]["close_ts"]
        events = envelope_events(econ_p, end_ts, totals_p["reason"],
                                 totals_p["final_turn"], totals_p["final_nav"], metrics_sha)
        for ev in events:
            errs = list(v_event.iter_errors(ev))
            assert not errs, (name, "event", ev["seq"], ev["type"], [e.message for e in errs[:5]])
        write_canonical(out / "events.jsonl", events)

        # -- ledgers, both profiles
        for profile, sim, fname in (("primary", prim, "ledger.jsonl"),
                                    ("stress_2x", stress, "ledger_stress_2x.jsonl")):
            rows = ledger_rows(sim[0], profile)
            assert rows[0]["nav_micro"] == START_NAV and rows[0]["turn"] is None
            for r in rows:
                errs = list(v_ledger.iter_errors(r))
                assert not errs, (name, fname, r["bar_index"], [e.message for e in errs[:5]])
            write_canonical(out / fname, rows)

        # -- re-verify MATH-2 + cash conservation + metrics cross-check on WRITTEN bytes
        for fname in ("ledger.jsonl", "ledger_stress_2x.jsonl"):
            rows = [json.loads(l) for l in (out / fname).read_text().splitlines()]
            for i, r in enumerate(rows):
                assert r["d_nav_micro"] == (r["d_price_pnl_micro"] + r["d_funding_micro"]
                                            + r["d_fees_micro"] + r["d_liq_penalty_micro"])
                if i:
                    p = rows[i - 1]
                    assert r["nav_micro"] - p["nav_micro"] == r["d_nav_micro"]
                    # cash changes only by realized components
                    assert (r["cash_micro"] - p["cash_micro"]
                            == r["realized_pnl_micro"] - p["realized_pnl_micro"])
                    assert (r["nav_micro"] == r["cash_micro"]
                            + r["positions"]["BTC"]["upnl_micro"])
        met = json.loads((out / "metrics.json").read_text())
        led = [json.loads(l) for l in (out / "ledger.jsonl").read_text().splitlines()]
        assert met["profiles"]["primary"]["net_return_1e8"] == floor_frac(
            Fraction((led[-1]["nav_micro"] - START_NAV) * E8, START_NAV))
        print(f"{name}: OK — {len(events)} events, {len(led)} ledger rows x2 profiles, "
              f"metrics_sha {metrics_sha[:23]}…")
        print(f"  stress_2x: final_nav {stress[3]['final_nav']}, fees {stress[3]['fees_cum']}, "
              f"funding {stress[3]['funding_cum']}, turnover {stress[3]['turnover']}, "
              f"penalty {stress[3]['penalty_cum']}")
        print(f"  primary metrics: {json.dumps(met['profiles']['primary'])}")
        print(f"  stress metrics:  {json.dumps(met['profiles']['stress_2x'])}")

    print("ALL EMIT + VALIDATE + INVARIANT CHECKS: PASS")


if __name__ == "__main__":
    main()
