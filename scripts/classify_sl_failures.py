"""SL failure mode classifier (read-only post-mortem).

Pulls SL outcomes for one or more setup types, computes MFE/MAE in R units
from 5m candles between actual fill and resolve, classifies each into a
failure mode, and optionally writes a markdown report.

Failure classes:
- wrong_direction:        MFE < 0.3R AND MAE >= 1.0R AND impulse_purity <= 0.85
- sl_too_tight_noise:     MFE >= 0.7R AND outcome SL
- late_entry:             MFE < 0.3R AND MAE >= 1.0R AND impulse_purity > 0.85
- wrong_zone:             htf_bias undefined at entry
- counter_trend_valid:    htf aligned, MFE in [0.3, 0.7], normal MAE
- unclassified:           none of the above

Run: PYTHONPATH=. python scripts/classify_sl_failures.py --limit 5
     PYTHONPATH=. python scripts/classify_sl_failures.py \
         --setups engine1_trend_pullback,setup_f --limit 100 \
         --report docs/audits/sl-postmortem-engine1-2026-05-20.md
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional

import psycopg2
import psycopg2.extras

from config.settings import settings


@dataclass
class SlRow:
    setup_id: str
    pair: str
    direction: str
    setup_type: str
    htf_bias: Optional[str]
    entry_price: float
    sl_price: float
    actual_entry: Optional[float]
    actual_exit: Optional[float]
    risk_distance_pct: Optional[float]
    fill_ts: int
    exit_ts: int
    impulse_purity: Optional[float]


@dataclass
class Excursion:
    mfe_abs: float
    mae_abs: float
    mfe_r: float
    mae_r: float
    candle_count: int


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _pull_sl_rows(cur, setup_type: str, limit: int, days: int) -> list[SlRow]:
    cur.execute(
        """
        SELECT setup_id, pair, direction, setup_type, htf_bias,
               entry_price, sl_price, actual_entry, actual_exit,
               risk_distance_pct,
               COALESCE(shadow_fill_candle_ts,
                        EXTRACT(EPOCH FROM created_at)*1000) AS fill_ts,
               COALESCE(shadow_resolve_candle_ts,
                        EXTRACT(EPOCH FROM resolved_at)*1000) AS exit_ts,
               impulse_directional_purity
        FROM ml_setups
        WHERE setup_type = %s
          AND outcome_type = 'shadow_sl'
          AND feature_version >= 4
          AND created_at >= NOW() - (%s || ' days')::interval
          AND COALESCE(shadow_resolve_candle_ts, 0) >
              COALESCE(shadow_fill_candle_ts, 0)
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (setup_type, days, limit),
    )
    rows = []
    for r in cur.fetchall():
        rows.append(SlRow(
            setup_id=r[0],
            pair=r[1],
            direction=r[2],
            setup_type=r[3],
            htf_bias=r[4],
            entry_price=float(r[5]) if r[5] is not None else None,
            sl_price=float(r[6]) if r[6] is not None else None,
            actual_entry=float(r[7]) if r[7] is not None else None,
            actual_exit=float(r[8]) if r[8] is not None else None,
            risk_distance_pct=float(r[9]) if r[9] is not None else None,
            fill_ts=int(r[10]) if r[10] is not None else 0,
            exit_ts=int(r[11]) if r[11] is not None else 0,
            impulse_purity=float(r[12]) if r[12] is not None else None,
        ))
    return rows


def _load_candles_between(cur, pair: str, tf: str, ts_start: int,
                          ts_end: int) -> list[tuple[int, float, float, float, float]]:
    """Return [(ts, open, high, low, close)] inclusive of fill candle,
    inclusive of resolve candle. Oldest-first."""
    cur.execute(
        """
        SELECT timestamp, open, high, low, close
        FROM candles
        WHERE pair = %s AND timeframe = %s
          AND timestamp >= %s AND timestamp <= %s
        ORDER BY timestamp ASC
        """,
        (pair, tf, ts_start, ts_end),
    )
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in cur.fetchall()]


def _compute_excursion(direction: str, entry: float, sl: float,
                       candles: list) -> Optional[Excursion]:
    if not candles or entry is None or sl is None or entry == sl:
        return None
    r_abs = abs(entry - sl)
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    if direction == "long":
        mfe_abs = max(highs) - entry
        mae_abs = entry - min(lows)
    elif direction == "short":
        mfe_abs = entry - min(lows)
        mae_abs = max(highs) - entry
    else:
        return None
    return Excursion(
        mfe_abs=mfe_abs,
        mae_abs=mae_abs,
        mfe_r=mfe_abs / r_abs,
        mae_r=mae_abs / r_abs,
        candle_count=len(candles),
    )


def _classify(row: SlRow, exc: Optional[Excursion]) -> str:
    if exc is None:
        return "unclassified"
    if row.htf_bias in (None, "undefined"):
        return "wrong_zone"
    if exc.mfe_r < 0.3 and exc.mae_r >= 1.0:
        # tight check for late entry: MFE near zero AND high impulse purity
        if row.impulse_purity is not None and row.impulse_purity > 0.85:
            return "late_entry"
        return "wrong_direction"
    if exc.mfe_r >= 0.7:
        return "sl_too_tight_noise"
    htf_aligned = (
        (row.direction == "long" and row.htf_bias == "bullish")
        or (row.direction == "short" and row.htf_bias == "bearish")
    )
    if 0.3 <= exc.mfe_r < 0.7 and htf_aligned:
        return "counter_trend_valid"
    return "unclassified"


@dataclass
class ClassifiedRow:
    setup_type: str
    pair: str
    direction: str
    htf_bias: Optional[str]
    entry: float
    sl: float
    risk_pct: float
    mfe_r: Optional[float]
    mae_r: Optional[float]
    duration_min: float
    candle_count: int
    cls: str
    setup_id: str


CLASS_ORDER = [
    "sl_too_tight_noise",
    "wrong_direction",
    "late_entry",
    "counter_trend_valid",
    "wrong_zone",
    "unclassified",
]


FIX_HYPOTHESIS = {
    "sl_too_tight_noise": (
        "Modal failure = SL inside noise distance. Price travels 0.7+R "
        "favorable then reverses and stops out. Candidate fixes: "
        "(a) raise ATR_SL_FLOOR_MULTIPLIER so SL sits beyond 5m wick noise; "
        "(b) restore TP1_RR=1.3 partial profit move (Batch 1) to convert these "
        "into BE rather than full losses; (c) require trigger-candle "
        "displacement > N×ATR before entry. Cost: 1-3 settings lines. NOT "
        "OSD-shaped — bias cascade does not help SLs that travel 0.7R favorable."
    ),
    "wrong_direction": (
        "Modal failure = trade went against intended direction immediately. "
        "Suggests bias detection at entry was wrong. Candidate fixes: "
        "(a) tighten HTF bias requirement (require 4H confirm not 1H-alone); "
        "(b) add 1D bias as veto layer. This is where partial-OSD reasoning "
        "starts to have edge — but only a 1D veto, not a 4-step cascade."
    ),
    "late_entry": (
        "Modal failure = entered at impulse terminus. Move was already done. "
        "Candidate fixes: tighten engine1 pullback depth threshold or add "
        "lookback that rejects entries within N candles of impulse peak."
    ),
    "wrong_zone": (
        "Modal failure = htf_bias undefined at entry (rare). Candidate fix: "
        "block setup emission when HTF state is undefined rather than allow."
    ),
    "counter_trend_valid": (
        "Modal failure = HTF aligned + reasonable MFE + still stopped out. "
        "These are unavoidable losses given current SL distance. Edge "
        "improvement here requires fee/sizing optimization, not detector tweak."
    ),
}


def _process_setup(cur, setup_type: str, limit: int, days: int,
                   tf: str) -> tuple[list[ClassifiedRow], int]:
    sl_rows = _pull_sl_rows(cur, setup_type, limit, days)
    sanity_failures = 0
    results = []
    for row in sl_rows:
        entry = row.actual_entry if row.actual_entry else row.entry_price
        candles = _load_candles_between(
            cur, row.pair, tf, row.fill_ts, row.exit_ts
        )
        exc = _compute_excursion(row.direction, entry, row.sl_price, candles)
        cls = _classify(row, exc)
        mins = (row.exit_ts - row.fill_ts) / 60_000 if row.exit_ts and row.fill_ts else 0
        if exc and exc.mae_r < 1.0:
            sanity_failures += 1
        results.append(ClassifiedRow(
            setup_type=setup_type,
            pair=row.pair,
            direction=row.direction,
            htf_bias=row.htf_bias,
            entry=entry,
            sl=row.sl_price,
            risk_pct=(row.risk_distance_pct or 0) * 100,
            mfe_r=exc.mfe_r if exc else None,
            mae_r=exc.mae_r if exc else None,
            duration_min=mins,
            candle_count=exc.candle_count if exc else len(candles),
            cls=cls,
            setup_id=row.setup_id,
        ))
    return results, sanity_failures


def _print_console_table(rows: list[ClassifiedRow]) -> None:
    header = (
        f"{'setup':<25} {'pair':<12} {'dir':<5} {'entry':>10} {'sl':>10} "
        f"{'R%':>6} {'MFE_R':>7} {'MAE_R':>7} {'mins':>5} "
        f"{'cnd':>4} {'class':<22}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        mfe_s = f"{r.mfe_r:.2f}" if r.mfe_r is not None else "N/A"
        mae_s = f"{r.mae_r:.2f}" if r.mae_r is not None else "N/A"
        print(
            f"{r.setup_type:<25} {r.pair:<12} {r.direction:<5} "
            f"{r.entry:>10.4f} {r.sl:>10.4f} "
            f"{r.risk_pct:>5.2f}% "
            f"{mfe_s:>7} {mae_s:>7} "
            f"{r.duration_min:>5.0f} {r.candle_count:>4} {r.cls:<22}"
        )


def _distribution(rows: list[ClassifiedRow]) -> dict[str, int]:
    d = {c: 0 for c in CLASS_ORDER}
    for r in rows:
        d[r.cls] = d.get(r.cls, 0) + 1
    return d


def _render_setup_section(setup_type: str, rows: list[ClassifiedRow]) -> str:
    if not rows:
        return f"### {setup_type}\n\nNo SL rows in window.\n\n"
    n = len(rows)
    dist = _distribution(rows)
    lines = [f"### {setup_type}", "", f"**N (SLs):** {n}", "",
             "**Class distribution:**", "",
             "| Class | Count | % |", "|---|---|---|"]
    for cls in CLASS_ORDER:
        cnt = dist.get(cls, 0)
        if cnt:
            pct = 100.0 * cnt / n
            lines.append(f"| {cls} | {cnt} | {pct:.1f}% |")
    lines.append("")
    # Top-3 by MAE per class
    by_cls: dict[str, list[ClassifiedRow]] = {}
    for r in rows:
        by_cls.setdefault(r.cls, []).append(r)
    lines.append("**Top examples per class (worst MAE):**")
    lines.append("")
    for cls in CLASS_ORDER:
        group = by_cls.get(cls, [])
        if not group:
            continue
        group.sort(key=lambda x: (x.mae_r or 0), reverse=True)
        lines.append(f"_{cls}_:")
        lines.append("")
        lines.append("| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in group[:3]:
            mfe_s = f"{r.mfe_r:.2f}" if r.mfe_r is not None else "N/A"
            mae_s = f"{r.mae_r:.2f}" if r.mae_r is not None else "N/A"
            lines.append(
                f"| {r.pair} | {r.direction} | {r.entry:.4f} | {r.sl:.4f} "
                f"| {r.risk_pct:.2f}% | {mfe_s} | {mae_s} | "
                f"{r.duration_min:.0f} |"
            )
        lines.append("")
    # Modal class hypothesis
    modal = max(dist, key=lambda k: dist[k])
    if dist[modal] > 0:
        lines.append(f"**Modal class:** `{modal}` ({dist[modal]}/{n} = "
                     f"{100.0 * dist[modal] / n:.1f}%)")
        lines.append("")
        lines.append(FIX_HYPOTHESIS.get(modal, "No hypothesis registered."))
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_report(per_setup: dict[str, list[ClassifiedRow]],
                   days: int, tf: str) -> str:
    today = "2026-05-20"
    header = [
        "# SL Post-Mortem — Engine 1 + setup_f",
        f"**Date:** {today}",
        f"**Window:** last {days} days",
        f"**Candle TF for MFE/MAE:** {tf}",
        "**Source plan:** docs/plans/_archive/sl-classifier-postmortem.md",
        "**Source grill:** docs/grill/_archive/one-step-down-cascade-2026-05-20.md",
        "",
        "## Methodology",
        "",
        "For each `shadow_sl` outcome, fetch 5m candles between fill_candle_ts "
        "and resolve_candle_ts. Compute Max Favorable Excursion (MFE) and Max "
        "Adverse Excursion (MAE) in R units, where R = |entry - sl|. Apply "
        "classifier in `scripts/classify_sl_failures.py`. Sanity: MAE must be "
        "≥1.0R for every SL row (since outcome=SL means price reached SL).",
        "",
        "## Class definitions",
        "",
        "- `sl_too_tight_noise`: MFE ≥ 0.7R — price travelled most of the way "
        "to TP1 then reversed. Suggests SL inside noise distance.",
        "- `wrong_direction`: MFE < 0.3R AND MAE ≥ 1.0R AND impulse_purity ≤ "
        "0.85 — market moved against trade immediately.",
        "- `late_entry`: MFE < 0.3R AND MAE ≥ 1.0R AND impulse_purity > 0.85 "
        "— entered at impulse terminus.",
        "- `wrong_zone`: htf_bias undefined at entry.",
        "- `counter_trend_valid`: HTF aligned, MFE in 0.3-0.7R range. Avoidable "
        "only via fee/sizing not detector.",
        "- `unclassified`: rules did not cover.",
        "",
        "## Results",
        "",
    ]
    body = []
    for st, rows in per_setup.items():
        body.append(_render_setup_section(st, rows))
    return "\n".join(header) + "\n" + "\n".join(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", default=None,
                        help="single setup type (legacy)")
    parser.add_argument("--setups", default=None,
                        help="comma-separated setup types")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--tf", default="5m")
    parser.add_argument("--report", default=None,
                        help="if set, write markdown report to this path")
    args = parser.parse_args()

    if args.setups:
        setups = [s.strip() for s in args.setups.split(",") if s.strip()]
    elif args.setup:
        setups = [args.setup]
    else:
        setups = ["engine1_trend_pullback"]

    conn = _connect()
    cur = conn.cursor()

    per_setup: dict[str, list[ClassifiedRow]] = {}
    total_sanity_failures = 0
    total_rows = 0
    total_classified = 0
    for st in setups:
        rows, sanity = _process_setup(cur, st, args.limit, args.days, args.tf)
        per_setup[st] = rows
        total_sanity_failures += sanity
        total_rows += len(rows)
        total_classified += sum(1 for r in rows if r.cls != "unclassified")

    if not args.report:
        # Console-only mode (Phase 1 style)
        flat = [r for rows in per_setup.values() for r in rows]
        if flat:
            _print_console_table(flat)
        else:
            print("No SL rows in window")
    else:
        report_text = _render_report(per_setup, args.days, args.tf)
        with open(args.report, "w") as f:
            f.write(report_text)
        print(f"Report written: {args.report}")

    print()
    pct_unclassified = (
        100.0 * (total_rows - total_classified) / total_rows
        if total_rows else 0
    )
    print(f"Total: {total_rows} | classified: {total_classified} | "
          f"unclassified: {total_rows - total_classified} "
          f"({pct_unclassified:.1f}%) | sanity_fail (MAE<1R): "
          f"{total_sanity_failures}")

    cur.close()
    conn.close()
    # Tolerate up to 5% sanity failures (residual data quality artifacts —
    # missing 5m candles around the resolve tick). Above that, classifier
    # output is unreliable and we should investigate.
    sanity_rate = total_sanity_failures / total_rows if total_rows else 0
    return 0 if sanity_rate < 0.05 else 1


if __name__ == "__main__":
    raise SystemExit(main())
