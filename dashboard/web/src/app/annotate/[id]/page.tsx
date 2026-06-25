"use client";

import { useEffect, useState, useCallback, use } from "react";
import Link from "next/link";
import { fetchApi, patchApi } from "@/lib/api";
import type { BybitAnnotation, BybitAnnotationPatch, BybitGradeExplain } from "@/lib/api";

const EMOTIONAL_STATES = ["calm", "confident", "FOMO", "revenge", "tired", "uncertain"];

const GRADE_COLORS: Record<string, string> = {
  A: "#b2fd02",
  B: "#9ca3af",
  C: "#f59e0b",
  D: "#ff4d4d",
};

// Journal v2 closed-vocab enums — keep in sync with bybit.py validators + schema.
const BIAS_OPTS = ["bullish", "bearish", "range"];
const STRUCT_REASON_OPTS = ["HH_HL", "LH_LL", "range_bound", "unclear"];
const LOCATION_PD_OPTS = ["premium", "equilibrium", "discount"];
const LOCATION_QUALITY_OPTS = ["key_level", "no_mans_land"];
const MTF_OPTS = ["confirms", "contradicts", "neutral"];
const LTF_TRIGGER_OPTS = ["sweep_reclaim", "bos", "choch", "fvg", "order_block", "simple_break"];
const STRUCTURE_TYPE_OPTS = ["continuation", "reversal", "range"];
const ENTRY_TYPE_OPTS = ["at_level_limit", "confirmation_shift"];

const TECHNICAL_ERRORS = [
  "misread_structure", "sl_bad_placement", "entered_against_htf",
  "early_no_confirmation", "wrong_invalidation", "chased_extended",
];
const BEHAVIORAL_ERRORS = [
  "outcome_bias", "inconsistent_sizing", "revenge_overtrade",
  "not_in_plan", "widened_sl", "cut_winner_early", "held_loser",
];

// The 5 confluence factors. HTF + trigger mandatory (range branch swaps HTF→location).
const CONF_FACTORS = [
  { key: "conf_htf", label: "HTF", hint: "4H/1D bias aligned with trade" },
  { key: "conf_location", label: "LOCATION", hint: "premium/discount + key level" },
  { key: "conf_mtf", label: "MTF 1H", hint: "1H confirms the trade" },
  { key: "conf_trigger", label: "TRIGGER", hint: "LTF entry trigger present" },
  { key: "conf_noconflict", label: "NO CONFLICT", hint: "funding/CVD/session not fighting" },
] as const;

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

type ParamsP = Promise<{ id: string }>;

export default function AnnotatePage({ params }: { params: ParamsP }) {
  const { id } = use(params);
  const annotationId = Number(id);
  const [annot, setAnnot] = useState<BybitAnnotation | null>(null);
  const [explain, setExplain] = useState<BybitGradeExplain | null>(null);
  const [showLegend, setShowLegend] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>("");

  const [thesis, setThesis] = useState("");
  const [trigger, setTrigger] = useState("");
  const [invalidation, setInvalidation] = useState("");
  const [lesson, setLesson] = useState("");
  const [emotional, setEmotional] = useState("");
  const [screenshot, setScreenshot] = useState("");
  const [topdownUsed, setTopdownUsed] = useState(false);
  const [isPractice, setIsPractice] = useState(false);

  // v2 top-down chain (selects). Empty string = unset.
  const [chain, setChain] = useState<Record<string, string>>({});
  // v2 confluence booleans
  const [conf, setConf] = useState<Record<string, boolean>>({});
  // v2 planned levels
  const [plannedEntry, setPlannedEntry] = useState("");
  const [plannedSl, setPlannedSl] = useState("");
  const [plannedTp, setPlannedTp] = useState("");
  const [riskPct, setRiskPct] = useState("");
  // v2 review
  const [followedProcess, setFollowedProcess] = useState<boolean | null>(null);
  const [techErrors, setTechErrors] = useState<string[]>([]);
  const [behavErrors, setBehavErrors] = useState<string[]>([]);

  const setChainField = (k: string, v: string) => setChain((c) => ({ ...c, [k]: v }));
  const toggleConf = (k: string) => setConf((c) => ({ ...c, [k]: !c[k] }));
  const toggleTag = (arr: string[], set: (v: string[]) => void, tag: string) =>
    set(arr.includes(tag) ? arr.filter((t) => t !== tag) : [...arr, tag]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const a = await fetchApi<BybitAnnotation>(`/bybit/annotations/${annotationId}`);
      setAnnot(a);
      setThesis(a.thesis_pre || "");
      setTrigger(a.trigger_condition || "");
      setInvalidation(a.thesis_invalidation || "");
      setLesson(a.lesson_post || "");
      setEmotional(a.emotional_state || "");
      setScreenshot(a.screenshot_url || "");
      setTopdownUsed(a.topdown_brief_used ?? false);
      setIsPractice(a.is_practice ?? false);

      // Chain: human value if set, else machine guess (auto_*), else blank.
      const pick = (human: unknown, auto: unknown) =>
        (human as string) || (auto as string) || "";
      setChain({
        htf_bias_daily: pick(a.htf_bias_daily, a.auto_htf_bias_daily),
        htf_bias_4h: pick(a.htf_bias_4h, a.auto_htf_bias_4h),
        htf_structure_reason: pick(a.htf_structure_reason, a.auto_htf_structure_reason),
        location_pd: pick(a.location_pd, a.auto_location_pd),
        location_quality: pick(a.location_quality, a.auto_location_quality),
        mtf_1h: pick(a.mtf_1h, a.auto_mtf_1h),
        ltf_trigger: pick(a.ltf_trigger, a.auto_ltf_trigger),
        structure_type: pick(a.structure_type, a.auto_structure_type),
        entry_type: a.entry_type || "",
      });
      const pickBool = (human: unknown, auto: unknown) =>
        human != null ? Boolean(human) : Boolean(auto);
      setConf({
        conf_htf: pickBool(a.conf_htf, a.auto_conf_htf),
        conf_location: pickBool(a.conf_location, a.auto_conf_location),
        conf_mtf: pickBool(a.conf_mtf, a.auto_conf_mtf),
        conf_trigger: pickBool(a.conf_trigger, a.auto_conf_trigger),
        conf_noconflict: pickBool(a.conf_noconflict, a.auto_conf_noconflict),
      });
      setPlannedEntry(a.planned_entry_price != null ? String(a.planned_entry_price) : "");
      setPlannedSl(a.planned_sl_price != null ? String(a.planned_sl_price) : "");
      setPlannedTp(a.planned_tp_price != null ? String(a.planned_tp_price) : "");
      setRiskPct(a.risk_pct != null ? String(a.risk_pct) : "");
      setFollowedProcess(a.followed_process ?? null);
      setTechErrors(a.technical_error || []);
      setBehavErrors(a.behavioral_error || []);
      if (a.auto_grade) {
        try {
          const ex = await fetchApi<BybitGradeExplain>(`/bybit/grade-explain/${annotationId}`);
          setExplain(ex);
        } catch {
          setExplain(null);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, [annotationId]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const num = (s: string) => (s.trim() === "" ? null : Number(s));
      const payload: BybitAnnotationPatch = {
        thesis_pre: thesis || null,
        trigger_condition: trigger || null,
        thesis_invalidation: invalidation || null,
        lesson_post: lesson || null,
        emotional_state: emotional || null,
        screenshot_url: screenshot || null,
        topdown_brief_used: topdownUsed,
        is_practice: isPractice,
        // v2 chain (empty select -> null)
        htf_bias_daily: chain.htf_bias_daily || null,
        htf_bias_4h: chain.htf_bias_4h || null,
        htf_structure_reason: chain.htf_structure_reason || null,
        location_pd: chain.location_pd || null,
        location_quality: chain.location_quality || null,
        mtf_1h: chain.mtf_1h || null,
        ltf_trigger: chain.ltf_trigger || null,
        structure_type: chain.structure_type || null,
        entry_type: chain.entry_type || null,
        conf_htf: !!conf.conf_htf,
        conf_location: !!conf.conf_location,
        conf_mtf: !!conf.conf_mtf,
        conf_trigger: !!conf.conf_trigger,
        conf_noconflict: !!conf.conf_noconflict,
        planned_entry_price: num(plannedEntry),
        planned_sl_price: num(plannedSl),
        planned_tp_price: num(plannedTp),
        risk_pct: num(riskPct),
        // v2 review — only send process verdict once chosen (blank-default honesty layer)
        ...(followedProcess != null ? { followed_process: followedProcess } : {}),
        technical_error: techErrors,
        behavioral_error: behavErrors,
      };
      const updated = await patchApi<BybitAnnotation>(
        `/bybit/annotations/${annotationId}`,
        payload,
      );
      setAnnot(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 2400);
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="annot-root">
        <div className="center-empty">— loading —</div>
        <style jsx global>{`.annot-root { min-height: 100vh; background: #050505; color: #f5f5f7; font-family: "JetBrains Mono", monospace; }`}</style>
        <style jsx>{`.center-empty { display: flex; align-items: center; justify-content: center; min-height: 60vh; color: rgba(255,255,255,0.4); letter-spacing: 0.2em; font-size: 12px; }`}</style>
      </div>
    );
  }
  if (!annot) {
    return (
      <div className="annot-root">
        <div className="center-empty">{error || "not found"}</div>
        <style jsx global>{`.annot-root { min-height: 100vh; background: #050505; color: #ff4d4d; font-family: "JetBrains Mono", monospace; }`}</style>
        <style jsx>{`.center-empty { display: flex; align-items: center; justify-content: center; min-height: 60vh; letter-spacing: 0.15em; font-size: 14px; }`}</style>
      </div>
    );
  }

  const ctx = annot.context_snapshot as Record<string, unknown> | null;
  const htf = (ctx?.htf_bias as Record<string, unknown> | undefined) || {};
  const warnings = (ctx?.warnings as string[] | undefined) || [];
  const isLong = annot.side === "Buy";
  const isClosed = annot.status === "closed";
  const pnl = annot.pnl_usd ?? 0;

  // 3-of-5 confluence gate. Range branch: when HTF bias is range there is no
  // direction, so the mandatory pair becomes trigger + location instead of HTF.
  const confCount = CONF_FACTORS.reduce((n, f) => n + (conf[f.key] ? 1 : 0), 0);
  const isRange = chain.htf_bias_4h === "range" || chain.htf_bias_daily === "range";
  const mandatoryOk = isRange
    ? !!conf.conf_trigger && !!conf.conf_location
    : !!conf.conf_htf && !!conf.conf_trigger;
  const gatePass = confCount >= 3 && mandatoryOk;
  const mandatoryLabel = isRange ? "trigger + location (range)" : "HTF + trigger";

  return (
    <div className="annot-root">
      <div className="grain" />

      <header className="a-head">
        <div className="breadcrumb">
          <Link href="/bybit" className="back">← LOG</Link>
          <span className="crumb-sep">/</span>
          <span className="crumb-id">#{annot.id}</span>
        </div>
        <div className="identity">
          <span className={`dir ${isLong ? "long" : "short"}`}>{isLong ? "LONG" : "SHORT"}</span>
          <h1 className="sym">{annot.symbol}</h1>
          <span className={`status ${isClosed ? "closed" : "live"}`}>
            {isClosed ? "CLOSED" : "● LIVE"}
          </span>
        </div>
        <div className="ticker">
          <TickerItem label="ENTRY" value={fmt(annot.entry_price, 4)} />
          <TickerItem label="SIZE" value={fmt(annot.size, 4)} />
          <TickerItem label="LEV" value={annot.leverage ? `×${fmt(annot.leverage, 0)}` : "—"} />
          {isClosed && <TickerItem label="EXIT" value={fmt(annot.exit_price, 4)} />}
          {isClosed && (
            <TickerItem
              label="P&L"
              value={`${pnl >= 0 ? "+" : ""}$${fmt(pnl)}`}
              className={pnl >= 0 ? "pos" : "neg"}
            />
          )}
          {isClosed && annot.pnl_pct != null && (
            <TickerItem
              label="ROI"
              value={`${fmt(annot.pnl_pct, 2)}%`}
              className={pnl >= 0 ? "pos" : "neg"}
            />
          )}
        </div>
      </header>

      {ctx && (
        <section className="ctx-block">
          <div className="ctx-eyebrow">CONTEXT AT ENTRY</div>
          <ul className="ctx-list">
            <li>
              <span className="cl">4H</span>
              <span className="cv">{(htf.bias_4h as string) || "?"}</span>
            </li>
            <li>
              <span className="cl">1H</span>
              <span className="cv">{(htf.bias_1h as string) || "?"}</span>
            </li>
            {htf.aligned_with_trade === true && <li className="ok">· HTF aligned</li>}
            {htf.aligned_with_trade === false && <li className="warn">· HTF counter</li>}
            {ctx.funding != null && (
              <li>
                <span className="cl">FUND</span>
                <span className="cv">{fmt(ctx.funding as number, 4)}%</span>
              </li>
            )}
            {ctx.oi_delta_1h_pct != null && (
              <li>
                <span className="cl">OI-1h</span>
                <span className="cv">{fmt(ctx.oi_delta_1h_pct as number, 2)}%</span>
              </li>
            )}
            {(ctx.cvd as Record<string, unknown> | undefined)?.cvd_1h != null && (
              <li>
                <span className="cl">CVD-1h</span>
                <span className="cv">{fmt((ctx.cvd as Record<string, number>).cvd_1h, 0)}</span>
              </li>
            )}
            {ctx.nearest_liq_cluster != null && (() => {
              const liq = ctx.nearest_liq_cluster as Record<string, unknown>;
              return (
                <li>
                  <span className="cl">LIQ {(liq.side as string)?.toUpperCase()}</span>
                  <span className="cv">@{fmt(liq.price as number, 2)} ({fmt(liq.distance_pct as number, 1)}%)</span>
                </li>
              );
            })()}
            {(ctx.volume_profile as Record<string, unknown> | undefined)?.zone != null && (() => {
              const vp = ctx.volume_profile as Record<string, unknown>;
              return (
                <li>
                  <span className="cl">VP</span>
                  <span className="cv">{String(vp.zone)} · POC {fmt(vp.poc as number, 2)}</span>
                </li>
              );
            })()}
            {(ctx.orderbook as Record<string, unknown> | undefined)?.imbalance_top20 != null && (() => {
              const ob = ctx.orderbook as Record<string, unknown>;
              return (
                <li>
                  <span className="cl">OB IMB</span>
                  <span className="cv">{fmt(ob.imbalance_top20 as number, 2)} · spread {fmt(ob.spread_bps as number, 1)}bps</span>
                </li>
              );
            })()}
            {(ctx.absorption as Record<string, unknown> | undefined)?.volume_ratio_5m != null && (() => {
              const ab = ctx.absorption as Record<string, unknown>;
              return (
                <li>
                  <span className="cl">VOL-5m</span>
                  <span className="cv">{fmt(ab.volume_ratio_5m as number, 2)}x{ab.absorption_detected ? " · absorption" : ab.displacement_detected ? " · displacement" : ""}</span>
                </li>
              );
            })()}
          </ul>
          {(() => {
            const smc = ctx.smc as Record<string, unknown> | undefined;
            if (!smc) return null;
            const obs = (smc.obs_nearest as Record<string, Record<string, unknown>> | undefined) || {};
            const sweeps = (smc.recent_sweeps as Array<Record<string, unknown>> | undefined) || [];
            const breaks = (smc.recent_breaks as Array<Record<string, unknown>> | undefined) || [];
            const tags: string[] = [];
            Object.entries(obs).forEach(([tf, ob]) => {
              const inZone = ob.in_zone;
              const dist = ob.distance_pct as number;
              tags.push(`OB ${tf} ${inZone ? "IN ZONE" : `${fmt(dist, 2)}%`}`);
            });
            sweeps.forEach((s) => tags.push(`sweep ${s.tf} ×${s.touch_count}`));
            breaks.forEach((b) => tags.push(`${String(b.type).toUpperCase()} ${b.tf}`));
            if (!tags.length) return null;
            return (
              <div className="smc-row">
                {tags.map((t, i) => <span key={i} className="smc-tag">{t}</span>)}
              </div>
            );
          })()}
          {warnings.length > 0 && (
            <div className="warn-row">
              {warnings.map((w, i) => <span key={i} className="warn-flag">⚠ {w}</span>)}
            </div>
          )}
        </section>
      )}

      {annot.auto_setup_type && (
        <section className="auto-block">
          <div className="auto-eyebrow-row">
            <span className="ctx-eyebrow">AUTO CLASSIFICATION · v{annot.auto_classifier_version ?? "?"}</span>
            <button
              type="button"
              className="legend-toggle"
              onClick={() => setShowLegend((v) => !v)}
              aria-expanded={showLegend}
            >
              {showLegend ? "hide legend ▴" : "what is this? ▾"}
            </button>
          </div>

          {showLegend && (
            <div className="legend">
              <div className="legend-title">How grading works</div>
              <p className="legend-body">
                <strong>net score = confluences − detractors.</strong> Decision quality at entry,
                not PnL. A trade can earn a D and still win — the score measures whether the
                structural context was present, nothing more.
              </p>
              <div className="legend-grid">
                <div><span className="g g-A">A</span> ≥ 6 net</div>
                <div><span className="g g-B">B</span> ≥ 4 net</div>
                <div><span className="g g-C">C</span> ≥ 2 net</div>
                <div><span className="g g-D">D</span> &lt; 2 net</div>
              </div>
              <p className="legend-foot">
                Full rubric: <code>docs/SYSTEM_BASELINE.md §10</code>. Detector code:{" "}
                <code>strategy_service/trade_classifier.py</code>.
              </p>
            </div>
          )}

          <div className="auto-head">
            <div className="auto-setup">
              <span className="al">SETUP</span>
              <span className="av">{annot.auto_setup_type}</span>
            </div>
            {annot.auto_grade && (
              <div className="auto-grade" style={{ color: GRADE_COLORS[annot.auto_grade] || "#fff" }}>
                <span className="al">GRADE</span>
                <span className="av" style={{ color: GRADE_COLORS[annot.auto_grade] || "#fff" }}>
                  {annot.auto_grade}
                </span>
              </div>
            )}
            <div className="auto-counts">
              <span className="pos">+{annot.auto_confluences?.length ?? 0}</span>
              <span className="neg">-{annot.auto_detractors?.length ?? 0}</span>
              {explain && <span className="net">net {explain.net_score >= 0 ? "+" : ""}{explain.net_score}</span>}
            </div>
          </div>

          {explain ? (
            <>
              {explain.confluences.length > 0 && (
                <div className="auto-explain">
                  <div className="explain-head">CONFLUENCES · {explain.confluences.length}</div>
                  <ul className="explain-list">
                    {explain.confluences.map((c) => (
                      <li key={c.tag} className="exp-item pos">
                        <span className="exp-tag">✓ {c.tag}</span>
                        <span className="exp-desc">{c.description}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {explain.detractors.length > 0 && (
                <div className="auto-explain">
                  <div className="explain-head">DETRACTORS · {explain.detractors.length}</div>
                  <ul className="explain-list">
                    {explain.detractors.map((c) => (
                      <li key={c.tag} className="exp-item neg">
                        <span className="exp-tag">✗ {c.tag}</span>
                        <span className="exp-desc">{c.description}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          ) : (
            <>
              {(annot.auto_confluences?.length ?? 0) > 0 && (
                <div className="auto-chips">
                  {annot.auto_confluences!.map((c) => (
                    <span key={c} className="auto-chip pos">✓ {c}</span>
                  ))}
                </div>
              )}
              {(annot.auto_detractors?.length ?? 0) > 0 && (
                <div className="auto-chips">
                  {annot.auto_detractors!.map((c) => (
                    <span key={c} className="auto-chip neg">✗ {c}</span>
                  ))}
                </div>
              )}
            </>
          )}
        </section>
      )}

      <main className="form">
        <div className="sec-eyebrow">PLAN · TOP-DOWN CHAIN</div>
        <p className="sec-note">
          Pre-filled from the auto-classifier — tap to confirm or correct. Your label is
          kept separately from the machine guess; a disagreement is the signal.
        </p>

        <div className="chain-grid">
          <Select label="DAILY BIAS" value={chain.htf_bias_daily || ""} opts={BIAS_OPTS}
            onChange={(v) => setChainField("htf_bias_daily", v)} auto={annot.auto_htf_bias_daily} />
          <Select label="4H BIAS" value={chain.htf_bias_4h || ""} opts={BIAS_OPTS}
            onChange={(v) => setChainField("htf_bias_4h", v)} auto={annot.auto_htf_bias_4h} />
          <Select label="HTF STRUCTURE" value={chain.htf_structure_reason || ""} opts={STRUCT_REASON_OPTS}
            onChange={(v) => setChainField("htf_structure_reason", v)} auto={annot.auto_htf_structure_reason} />
          <Select label="1H (MTF)" value={chain.mtf_1h || ""} opts={MTF_OPTS}
            onChange={(v) => setChainField("mtf_1h", v)} auto={annot.auto_mtf_1h} />
          <Select label="LOCATION · PD" value={chain.location_pd || ""} opts={LOCATION_PD_OPTS}
            onChange={(v) => setChainField("location_pd", v)} auto={annot.auto_location_pd} />
          <Select label="LOCATION · QUALITY" value={chain.location_quality || ""} opts={LOCATION_QUALITY_OPTS}
            onChange={(v) => setChainField("location_quality", v)} auto={annot.auto_location_quality} />
          <Select label="LTF TRIGGER" value={chain.ltf_trigger || ""} opts={LTF_TRIGGER_OPTS}
            onChange={(v) => setChainField("ltf_trigger", v)} auto={annot.auto_ltf_trigger} />
          <Select label="STRUCTURE TYPE" value={chain.structure_type || ""} opts={STRUCTURE_TYPE_OPTS}
            onChange={(v) => setChainField("structure_type", v)} auto={annot.auto_structure_type} />
          <Select label="ENTRY TYPE" value={chain.entry_type || ""} opts={ENTRY_TYPE_OPTS}
            onChange={(v) => setChainField("entry_type", v)} auto={null} />
        </div>

        <div className="sec-eyebrow mt">PLAN · CONFLUENCE CHECKLIST</div>
        <div className="conf-grid">
          {CONF_FACTORS.map((f) => (
            <button
              type="button"
              key={f.key}
              className={`conf-box ${conf[f.key] ? "on" : ""}`}
              onClick={() => toggleConf(f.key)}
              aria-pressed={!!conf[f.key]}
            >
              <span className="conf-check" aria-hidden="true" />
              <span className="conf-label">{f.label}</span>
              <span className="conf-hint">{f.hint}</span>
            </button>
          ))}
        </div>
        <div className={`gate ${gatePass ? "pass" : "fail"}`}>
          <span className="gate-count">{confCount}/5</span>
          <span className="gate-text">
            {gatePass ? "✓ meets 3-of-5 floor" : "below floor"} · mandatory: {mandatoryLabel}
          </span>
        </div>

        <div className="sec-eyebrow mt">PLAN · INTENDED LEVELS</div>
        <p className="sec-note">
          Auto-captured from your Bybit order at entry. Edit only to correct — the
          gap vs the executed exit is what reveals widened-SL / cut-winner.
        </p>
        <div className="lvl-grid">
          <NumField label="ENTRY" value={plannedEntry} onChange={setPlannedEntry} placeholder="79250" />
          <NumField label="STOP LOSS" value={plannedSl} onChange={setPlannedSl} placeholder="78400" />
          <NumField label="TAKE PROFIT" value={plannedTp} onChange={setPlannedTp} placeholder="81500" />
          <NumField label="RISK %" value={riskPct} onChange={setRiskPct} placeholder="1.0" />
        </div>

        <PlanVsExecuted annot={annot} />

        <div className="sec-eyebrow mt">JOURNAL · NOTES</div>

        <Field label="EMOTIONAL STATE (OPTIONAL)">
          <div className="chips">
            <button
              type="button"
              className={`chip ${!emotional ? "on" : ""}`}
              onClick={() => setEmotional("")}
            >
              none
            </button>
            {EMOTIONAL_STATES.map((e) => (
              <button
                type="button"
                key={e}
                className={`chip ${emotional === e ? "on" : ""}`}
                onClick={() => setEmotional(e)}
              >
                {e}
              </button>
            ))}
          </div>
        </Field>

        <label className="topdown-check">
          <input
            type="checkbox"
            checked={topdownUsed}
            onChange={(e) => setTopdownUsed(e.target.checked)}
          />
          <span className="tc-box" aria-hidden="true" />
          <span className="tc-text">USED /topdown BRIEF BEFORE ENTRY</span>
        </label>

        <label className="topdown-check">
          <input
            type="checkbox"
            checked={isPractice}
            onChange={(e) => setIsPractice(e.target.checked)}
          />
          <span className="tc-box" aria-hidden="true" />
          <span className="tc-text">PRACTICE TRADE (micro size — excluded from edge math)</span>
        </label>

        <Field label="TRIGGER · WHAT FIRED THE ENTRY (RULE 1)">
          <textarea
            rows={3}
            value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
            placeholder="e.g. rebote en POC 4H 79.2k con vela cuerpo entero + RSI<30 5m"
          />
        </Field>

        <Field label="INVALIDATION · WHAT BREAKS THESIS (RULE 11)">
          <textarea
            rows={3}
            value={invalidation}
            onChange={(e) => setInvalidation(e.target.value)}
            placeholder="e.g. cierre 15m > 80.1k = thesis short rota (distinto del SL price)"
          />
        </Field>

        <Field label="THESIS / WHY I ENTERED (OPTIONAL)">
          <textarea
            rows={4}
            value={thesis}
            onChange={(e) => setThesis(e.target.value)}
            placeholder="lo que vi, por qué tomé el trade…"
          />
        </Field>

        {isClosed && (
          <>
            <div className="sec-eyebrow mt">REVIEW · PROCESS</div>
            <p className="sec-note">
              Blank by default — only mark once you&apos;ve honestly reviewed. Drives the
              ML clean-sample label; rule-break trades are excluded from edge math.
            </p>
            <div className="proc-row">
              <span className="proc-q">Followed your process?</span>
              <div className="proc-toggle">
                <button
                  type="button"
                  className={`pt yes ${followedProcess === true ? "on" : ""}`}
                  onClick={() => setFollowedProcess(followedProcess === true ? null : true)}
                >
                  YES
                </button>
                <button
                  type="button"
                  className={`pt no ${followedProcess === false ? "on" : ""}`}
                  onClick={() => setFollowedProcess(followedProcess === false ? null : false)}
                >
                  NO
                </button>
              </div>
            </div>

            <Field label="TECHNICAL ERRORS (IF ANY)">
              <div className="chips">
                {TECHNICAL_ERRORS.map((t) => (
                  <button
                    type="button"
                    key={t}
                    className={`chip ${techErrors.includes(t) ? "on" : ""}`}
                    onClick={() => toggleTag(techErrors, setTechErrors, t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </Field>

            <Field label="BEHAVIORAL ERRORS (IF ANY)">
              <div className="chips">
                {BEHAVIORAL_ERRORS.map((t) => (
                  <button
                    type="button"
                    key={t}
                    className={`chip warn ${behavErrors.includes(t) ? "on" : ""}`}
                    onClick={() => toggleTag(behavErrors, setBehavErrors, t)}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </Field>

            <Field label="LESSON / WHAT I'D DO DIFFERENT">
              <textarea
                rows={4}
                value={lesson}
                onChange={(e) => setLesson(e.target.value)}
                placeholder="post-mortem honesto…"
              />
            </Field>
          </>
        )}

        <Field label="SCREENSHOT URL (OPTIONAL)">
          <input
            type="url"
            value={screenshot}
            onChange={(e) => setScreenshot(e.target.value)}
            placeholder="https://tradingview.com/x/…"
          />
        </Field>

        <div className="save-row">
          <button className={`save ${saved ? "done" : ""}`} onClick={save} disabled={saving}>
            {saving ? "SAVING…" : saved ? "✓ SAVED" : "SAVE ANNOTATION →"}
          </button>
          {error && <p className="err">ERROR · {error}</p>}
        </div>
      </main>

      <style jsx global>{`
        .annot-root {
          min-height: 100vh;
          background: #050505;
          color: #f5f5f7;
          font-family: "JetBrains Mono", monospace;
          position: relative;
          overflow-x: hidden;
        }
        .annot-root .serif { font-family: "Fraunces", Georgia, serif; }
        .annot-root .grain {
          position: fixed; inset: 0; pointer-events: none; z-index: 100;
          opacity: 0.035;
          background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
        }
      `}</style>
      <style jsx>{`
        .a-head {
          padding: 32px 24px 20px 24px;
          max-width: 720px;
          margin: 0 auto;
          animation: fade 0.6s ease both;
        }
        .breadcrumb {
          display: flex;
          gap: 10px;
          align-items: center;
          font-size: 11px;
          letter-spacing: 0.15em;
          color: rgba(255, 255, 255, 0.4);
          margin-bottom: 24px;
        }
        .back { color: rgba(255,255,255,0.6); text-decoration: none; font-weight: 600; transition: color 0.2s; }
        .back:hover { color: #b2fd02; }
        .crumb-sep { color: rgba(255,255,255,0.25); }

        .identity {
          display: flex;
          align-items: baseline;
          gap: 14px;
          flex-wrap: wrap;
        }
        .dir {
          font-size: 10px;
          font-weight: 700;
          letter-spacing: 0.2em;
          padding: 4px 10px;
          border-radius: 2px;
        }
        .dir.long { color: #b2fd02; background: rgba(178,253,2,0.1); }
        .dir.short { color: #ff4d4d; background: rgba(255,77,77,0.1); }
        .sym {
          font-family: "Instrument Serif", Georgia, serif;
          font-size: clamp(36px, 7vw, 64px);
          font-weight: 400;
          letter-spacing: -0.02em;
          line-height: 1;
          margin: 0;
          color: #f5f5f7;
        }
        .status {
          font-size: 10px;
          letter-spacing: 0.18em;
          font-weight: 700;
          padding: 3px 9px;
          border-radius: 2px;
        }
        .status.live { color: #b2fd02; background: rgba(178,253,2,0.08); animation: pulse 1.6s ease-in-out infinite; }
        .status.closed { color: rgba(255,255,255,0.5); background: rgba(255,255,255,0.06); }
        @keyframes pulse { 0%,100% { opacity: 0.7; } 50% { opacity: 1; } }

        .ticker {
          margin-top: 22px;
          display: flex;
          gap: 0;
          flex-wrap: wrap;
          border-top: 1px solid rgba(255, 255, 255, 0.08);
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }

        .ctx-block {
          max-width: 720px;
          margin: 0 auto;
          padding: 24px;
          animation: fade 0.7s 0.1s ease both;
        }
        .ctx-eyebrow {
          font-size: 9px;
          letter-spacing: 0.24em;
          color: rgba(255,255,255,0.35);
          font-weight: 600;
          margin-bottom: 12px;
        }
        .ctx-list {
          list-style: none;
          margin: 0; padding: 0;
          display: flex;
          flex-wrap: wrap;
          gap: 16px 22px;
          font-size: 12px;
        }
        .ctx-list li { display: inline-flex; gap: 8px; align-items: baseline; }
        .cl { font-size: 9px; letter-spacing: 0.15em; color: rgba(255,255,255,0.4); }
        .cv { font-weight: 500; color: rgba(255,255,255,0.85); font-family: "JetBrains Mono", monospace; }
        .ctx-list li.ok { color: #b2fd02; letter-spacing: 0.08em; }
        .ctx-list li.warn { color: #f59e0b; letter-spacing: 0.08em; }
        .warn-row {
          margin-top: 14px;
          display: flex; flex-wrap: wrap; gap: 6px;
        }
        .warn-flag {
          font-size: 11px;
          color: #f59e0b;
          background: rgba(245, 158, 11, 0.08);
          padding: 3px 9px;
          border-radius: 2px;
          letter-spacing: 0.03em;
        }
        .smc-row {
          margin-top: 14px;
          display: flex; flex-wrap: wrap; gap: 6px;
        }
        .smc-tag {
          font-family: "JetBrains Mono", monospace;
          font-size: 10px;
          letter-spacing: 0.05em;
          color: rgba(178, 253, 2, 0.85);
          border: 1px solid rgba(178, 253, 2, 0.2);
          background: rgba(178, 253, 2, 0.04);
          padding: 3px 9px;
          border-radius: 2px;
        }

        .auto-block {
          max-width: 720px;
          margin: 0 auto;
          padding: 4px 24px 20px 24px;
          animation: fade 0.75s 0.12s ease both;
        }
        .auto-head {
          display: flex;
          gap: 28px;
          align-items: flex-end;
          padding: 14px 16px;
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 2px;
          background: rgba(255,255,255,0.02);
        }
        .auto-setup, .auto-grade {
          display: flex;
          flex-direction: column;
          gap: 4px;
        }
        .auto-head .al { font-size: 9px; letter-spacing: 0.2em; color: rgba(255,255,255,0.4); }
        .auto-head .av {
          font-family: "Fraunces", serif;
          font-size: 22px;
          letter-spacing: -0.01em;
          line-height: 1;
        }
        .auto-grade .av { font-weight: 600; font-size: 26px; }
        .auto-counts {
          margin-left: auto;
          display: flex;
          gap: 10px;
          font-family: "JetBrains Mono", monospace;
          font-size: 12px;
          letter-spacing: 0.1em;
        }
        .auto-counts .pos { color: #b2fd02; }
        .auto-counts .neg { color: #ff4d4d; }
        .auto-chips {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          margin-top: 12px;
        }
        .auto-chip {
          font-family: "JetBrains Mono", monospace;
          font-size: 10px;
          letter-spacing: 0.03em;
          padding: 4px 10px;
          border-radius: 2px;
          border: 1px solid rgba(255,255,255,0.08);
        }
        .auto-chip.pos { color: #b2fd02; background: rgba(178,253,2,0.05); border-color: rgba(178,253,2,0.2); }
        .auto-chip.neg { color: #ff4d4d; background: rgba(255,77,77,0.05); border-color: rgba(255,77,77,0.2); }

        .auto-eyebrow-row {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 12px;
          flex-wrap: wrap;
          margin-bottom: 8px;
        }
        .legend-toggle {
          background: transparent;
          border: 1px solid rgba(255,255,255,0.16);
          color: rgba(255,255,255,0.55);
          font-family: "JetBrains Mono", monospace;
          font-size: 10px;
          letter-spacing: 0.08em;
          padding: 4px 10px;
          border-radius: 2px;
          cursor: pointer;
          transition: all 0.15s;
        }
        .legend-toggle:hover { color: #fff; border-color: rgba(255,255,255,0.32); }
        .legend {
          margin-bottom: 12px;
          padding: 14px 16px;
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.1);
          border-radius: 2px;
        }
        .legend-title {
          font-size: 10px;
          letter-spacing: 0.18em;
          color: rgba(255,255,255,0.55);
          font-weight: 700;
          margin-bottom: 8px;
        }
        .legend-body {
          margin: 0 0 10px 0;
          font-family: "Fraunces", Georgia, serif;
          font-style: italic;
          font-weight: 300;
          font-size: 14px;
          line-height: 1.5;
          color: rgba(255,255,255,0.78);
        }
        .legend-body strong {
          font-style: normal;
          font-weight: 500;
          font-family: "JetBrains Mono", monospace;
          font-size: 12px;
          color: #fff;
          letter-spacing: 0.04em;
        }
        .legend-grid {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 8px;
          font-family: "JetBrains Mono", monospace;
          font-size: 11px;
          color: rgba(255,255,255,0.65);
          margin-bottom: 10px;
        }
        .legend-grid .g {
          display: inline-block;
          font-family: "Fraunces", serif;
          font-weight: 700;
          font-size: 14px;
          padding: 1px 6px;
          margin-right: 6px;
          border-radius: 2px;
        }
        .legend-grid .g-A { color: #b2fd02; background: rgba(178,253,2,0.12); }
        .legend-grid .g-B { color: #9ca3af; background: rgba(156,163,175,0.15); }
        .legend-grid .g-C { color: #f59e0b; background: rgba(245,158,11,0.12); }
        .legend-grid .g-D { color: #ff4d4d; background: rgba(255,77,77,0.12); }
        .legend-foot {
          margin: 0;
          font-size: 10px;
          color: rgba(255,255,255,0.4);
          letter-spacing: 0.04em;
        }
        .legend-foot code {
          font-family: "JetBrains Mono", monospace;
          color: rgba(255,255,255,0.65);
          font-size: 10px;
        }
        .auto-counts .net {
          color: rgba(255,255,255,0.55);
          padding-left: 8px;
          border-left: 1px solid rgba(255,255,255,0.12);
        }
        .auto-explain {
          margin-top: 14px;
        }
        .explain-head {
          font-size: 9px;
          letter-spacing: 0.2em;
          color: rgba(255,255,255,0.4);
          font-weight: 700;
          margin-bottom: 6px;
        }
        .explain-list {
          list-style: none;
          margin: 0;
          padding: 0;
          display: flex;
          flex-direction: column;
          gap: 6px;
        }
        .exp-item {
          display: grid;
          grid-template-columns: 220px 1fr;
          gap: 12px;
          padding: 8px 12px;
          border-radius: 2px;
          font-size: 12px;
          line-height: 1.4;
          border: 1px solid rgba(255,255,255,0.06);
        }
        .exp-item.pos {
          background: rgba(178,253,2,0.04);
          border-color: rgba(178,253,2,0.18);
        }
        .exp-item.neg {
          background: rgba(255,77,77,0.04);
          border-color: rgba(255,77,77,0.18);
        }
        .exp-tag {
          font-family: "JetBrains Mono", monospace;
          font-size: 11px;
          font-weight: 600;
          letter-spacing: 0.02em;
        }
        .exp-item.pos .exp-tag { color: #b2fd02; }
        .exp-item.neg .exp-tag { color: #ff4d4d; }
        .exp-desc {
          color: rgba(255,255,255,0.78);
          font-family: "Fraunces", Georgia, serif;
          font-weight: 300;
          font-size: 13px;
          line-height: 1.45;
        }
        @media (max-width: 639px) {
          .legend-grid { grid-template-columns: repeat(2, 1fr); }
          .exp-item {
            grid-template-columns: 1fr;
            gap: 4px;
          }
          .exp-desc { font-size: 12px; }
        }

        .form {
          max-width: 720px;
          margin: 0 auto;
          padding: 10px 24px 80px 24px;
          animation: fade 0.8s 0.15s ease both;
        }
        .sec-eyebrow {
          font-size: 9px;
          letter-spacing: 0.24em;
          color: #b2fd02;
          font-weight: 700;
          margin-bottom: 8px;
        }
        .sec-eyebrow.mt { margin-top: 34px; }
        .sec-note {
          margin: 0 0 16px 0;
          font-family: "Fraunces", Georgia, serif;
          font-style: italic;
          font-weight: 300;
          font-size: 13px;
          line-height: 1.5;
          color: rgba(255,255,255,0.55);
        }
        .chain-grid {
          display: grid;
          grid-template-columns: repeat(3, 1fr);
          gap: 14px;
        }
        .conf-grid {
          display: grid;
          grid-template-columns: repeat(5, 1fr);
          gap: 8px;
        }
        .conf-box {
          display: flex;
          flex-direction: column;
          gap: 6px;
          padding: 12px 10px;
          background: rgba(255,255,255,0.03);
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 2px;
          cursor: pointer;
          text-align: left;
          min-height: 44px;
          transition: all 0.15s;
        }
        .conf-box:hover { border-color: rgba(255,255,255,0.3); }
        .conf-box.on { background: rgba(178,253,2,0.06); border-color: rgba(178,253,2,0.5); }
        .conf-check {
          width: 16px; height: 16px;
          border: 1px solid rgba(255,255,255,0.3);
          border-radius: 3px;
          position: relative;
        }
        .conf-box.on .conf-check { background: #b2fd02; border-color: #b2fd02; }
        .conf-box.on .conf-check::after {
          content: ""; position: absolute; left: 5px; top: 1px;
          width: 4px; height: 9px; border: solid #000; border-width: 0 2px 2px 0;
          transform: rotate(45deg);
        }
        .conf-label {
          font-family: "JetBrains Mono", monospace;
          font-size: 10px; font-weight: 700; letter-spacing: 0.08em;
          color: #f5f5f7;
        }
        .conf-hint { font-size: 9px; line-height: 1.35; color: rgba(255,255,255,0.4); }
        .gate {
          display: flex; align-items: center; gap: 12px;
          margin-top: 12px;
          padding: 10px 14px;
          border-radius: 2px;
          border: 1px solid;
          font-family: "JetBrains Mono", monospace;
        }
        .gate.pass { background: rgba(178,253,2,0.06); border-color: rgba(178,253,2,0.4); }
        .gate.fail { background: rgba(245,158,11,0.06); border-color: rgba(245,158,11,0.35); }
        .gate-count { font-size: 16px; font-weight: 700; }
        .gate.pass .gate-count { color: #b2fd02; }
        .gate.fail .gate-count { color: #f59e0b; }
        .gate-text { font-size: 10px; letter-spacing: 0.06em; color: rgba(255,255,255,0.7); }
        .lvl-grid {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 14px;
        }
        .proc-row {
          display: flex; align-items: center; justify-content: space-between;
          gap: 16px; flex-wrap: wrap;
          margin-bottom: 24px;
        }
        .proc-q {
          font-family: "Fraunces", Georgia, serif;
          font-size: 16px; color: rgba(255,255,255,0.85);
        }
        .proc-toggle { display: flex; gap: 8px; }
        .pt {
          padding: 10px 22px;
          background: transparent;
          border: 1px solid rgba(255,255,255,0.16);
          color: rgba(255,255,255,0.55);
          font-family: "JetBrains Mono", monospace;
          font-size: 12px; font-weight: 700; letter-spacing: 0.1em;
          border-radius: 2px; cursor: pointer; min-height: 44px;
          transition: all 0.15s;
        }
        .pt.yes.on { background: #b2fd02; color: #000; border-color: #b2fd02; }
        .pt.no.on { background: #ff4d4d; color: #000; border-color: #ff4d4d; }

        @media (max-width: 639px) {
          .chain-grid { grid-template-columns: repeat(2, 1fr); }
          .conf-grid { grid-template-columns: repeat(2, 1fr); }
          .lvl-grid { grid-template-columns: repeat(2, 1fr); }
        }

        .save-row { margin-top: 28px; }
        .save {
          width: 100%;
          padding: 18px;
          background: #b2fd02;
          color: #000;
          border: none;
          font-family: "JetBrains Mono", monospace;
          font-weight: 700;
          font-size: 13px;
          letter-spacing: 0.16em;
          cursor: pointer;
          border-radius: 0;
          transition: all 0.18s;
        }
        .save:hover { background: #c8ff4a; transform: translateY(-1px); }
        .save:disabled { opacity: 0.6; cursor: wait; }
        .save.done { background: transparent; color: #b2fd02; border: 1px solid #b2fd02; }
        .err {
          margin-top: 12px;
          color: #ff4d4d;
          font-size: 11px;
          letter-spacing: 0.12em;
        }

        @keyframes fade {
          from { opacity: 0; transform: translateY(6px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .topdown-check {
          display: flex;
          align-items: center;
          gap: 12px;
          min-height: 44px;
          margin-bottom: 24px;
          cursor: pointer;
          user-select: none;
        }
        .topdown-check input {
          position: absolute;
          opacity: 0;
          width: 0;
          height: 0;
        }
        .tc-box {
          flex: 0 0 auto;
          width: 22px;
          height: 22px;
          border: 1px solid rgba(255, 255, 255, 0.25);
          border-radius: 4px;
          background: rgba(255, 255, 255, 0.03);
          position: relative;
          transition: all 0.15s ease;
        }
        .topdown-check input:checked + .tc-box {
          background: #0a84ff;
          border-color: #0a84ff;
        }
        .topdown-check input:checked + .tc-box::after {
          content: "";
          position: absolute;
          left: 7px;
          top: 3px;
          width: 5px;
          height: 10px;
          border: solid #fff;
          border-width: 0 2px 2px 0;
          transform: rotate(45deg);
        }
        .topdown-check input:focus-visible + .tc-box {
          box-shadow: 0 0 0 3px rgba(10, 132, 255, 0.4);
        }
        .tc-text {
          font-size: 9px;
          letter-spacing: 0.22em;
          color: rgba(255, 255, 255, 0.7);
          font-weight: 700;
        }
        @media (max-width: 640px) {
          .a-head { padding: 24px 16px 16px; }
          .ctx-block, .form { padding-left: 16px; padding-right: 16px; }
        }
      `}</style>
    </div>
  );
}

function TickerItem({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className={`tk ${className || ""}`}>
      <div className="tk-l">{label}</div>
      <div className="tk-v">{value}</div>
      <style jsx>{`
        .tk {
          flex: 1 1 100px;
          padding: 14px 18px;
          border-right: 1px solid rgba(255, 255, 255, 0.08);
          min-width: 100px;
        }
        .tk:last-child { border-right: none; }
        .tk-l { font-size: 9px; letter-spacing: 0.2em; color: rgba(255,255,255,0.35); margin-bottom: 6px; }
        .tk-v {
          font-family: "Fraunces", serif;
          font-size: 20px;
          font-weight: 500;
          letter-spacing: -0.02em;
          font-feature-settings: "tnum" 1;
        }
        .tk.pos .tk-v { color: #b2fd02; }
        .tk.neg .tk-v { color: #ff4d4d; }
      `}</style>
    </div>
  );
}

function Select({
  label, value, opts, onChange, auto,
}: {
  label: string;
  value: string;
  opts: string[];
  onChange: (v: string) => void;
  auto?: string | null;
}) {
  const diverged = !!auto && !!value && value !== auto;
  return (
    <label className="sel">
      <span className="sel-l">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">—</option>
        {opts.map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
      {auto && !value && <span className="sel-auto">auto: {auto}</span>}
      {diverged && <span className="sel-auto diverge">≠ auto: {auto}</span>}
      <style jsx>{`
        .sel { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
        .sel-l {
          font-size: 9px; letter-spacing: 0.18em; color: rgba(255,255,255,0.45);
          font-weight: 700;
        }
        .sel select {
          width: 100%;
          box-sizing: border-box;
          background: rgba(255,255,255,0.03);
          color: #f5f5f7;
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 2px;
          padding: 11px 12px;
          font-family: "JetBrains Mono", monospace;
          font-size: 13px;
          min-height: 44px;
          appearance: none;
          -webkit-appearance: none;
          background-image: linear-gradient(45deg, transparent 50%, rgba(255,255,255,0.4) 50%), linear-gradient(135deg, rgba(255,255,255,0.4) 50%, transparent 50%);
          background-position: calc(100% - 16px) center, calc(100% - 11px) center;
          background-size: 5px 5px, 5px 5px;
          background-repeat: no-repeat;
        }
        .sel select:focus { outline: none; border-color: #b2fd02; background-color: rgba(178,253,2,0.03); }
        .sel-auto { font-size: 9px; letter-spacing: 0.06em; color: rgba(255,255,255,0.35); }
        .sel-auto.diverge { color: #f59e0b; }
      `}</style>
    </label>
  );
}

function NumField({
  label, value, onChange, placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="nf">
      <span className="nf-l">{label}</span>
      <input
        type="number"
        inputMode="decimal"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
      <style jsx>{`
        .nf { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
        .nf-l { font-size: 9px; letter-spacing: 0.18em; color: rgba(255,255,255,0.45); font-weight: 700; }
        .nf input {
          width: 100%;
          box-sizing: border-box;
          background: rgba(255,255,255,0.03);
          color: #f5f5f7;
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 2px;
          padding: 11px 12px;
          font-family: "JetBrains Mono", monospace;
          font-size: 13px;
          min-height: 44px;
        }
        .nf input:focus { outline: none; border-color: #b2fd02; background: rgba(178,253,2,0.03); }
        .nf input::placeholder { color: rgba(255,255,255,0.22); }
      `}</style>
    </label>
  );
}

function PlanVsExecuted({ annot }: { annot: BybitAnnotation }) {
  const entry = annot.entry_price ?? annot.planned_entry_price ?? null;
  const planned = annot.planned_sl_price ?? null;
  const live = annot.position_sl_price ?? null;
  const tookPartial = annot.took_partial;
  const movedBe = annot.moved_to_be;

  // SL delta: positive = stop moved AWAY from entry (widened = held loser); negative
  // = stop moved toward/past entry (tightened / break-even).
  let slVerdict: { text: string; cls: string } | null = null;
  if (entry != null && planned != null && live != null) {
    const plannedDist = Math.abs(planned - entry);
    const liveDist = Math.abs(live - entry);
    const diff = liveDist - plannedDist;
    const rel = plannedDist > 0 ? (diff / plannedDist) * 100 : 0;
    if (Math.abs(rel) < 1) {
      slVerdict = { text: "SL held as planned", cls: "ok" };
    } else if (diff > 0) {
      slVerdict = { text: `SL widened ${rel.toFixed(0)}% (held loser?)`, cls: "bad" };
    } else {
      // tightened — distinguish break-even from a normal trail using movedBe flag
      slVerdict = {
        text: movedBe ? "SL → break-even" : `SL tightened ${Math.abs(rel).toFixed(0)}%`,
        cls: "ok",
      };
    }
  }

  const badges: { text: string; cls: string }[] = [];
  if (slVerdict) badges.push(slVerdict);
  if (tookPartial != null)
    badges.push({ text: tookPartial ? "took partial" : "no partial", cls: tookPartial ? "ok" : "neutral" });
  if (movedBe != null && !slVerdict)
    badges.push({ text: movedBe ? "moved to BE" : "stop not moved", cls: movedBe ? "ok" : "neutral" });

  if (badges.length === 0) return null;

  return (
    <div className="pve">
      <div className="sec-eyebrow">PLAN vs EXECUTED · auto</div>
      <div className="pve-row">
        {badges.map((b, i) => (
          <span key={i} className={`pve-badge ${b.cls}`}>{b.text}</span>
        ))}
      </div>
      <style jsx>{`
        .pve { margin: 4px 0 24px; }
        .pve-row { display: flex; flex-wrap: wrap; gap: 8px; }
        .pve-badge {
          font-size: 11px; padding: 5px 10px; border-radius: 100px;
          font-weight: 600; letter-spacing: 0.02em;
          border: 1px solid rgba(255,255,255,0.12);
        }
        .pve-badge.ok { background: rgba(48,209,88,0.12); color: #4ade80; }
        .pve-badge.bad { background: rgba(255,69,58,0.12); color: #ff6b6b; }
        .pve-badge.neutral { background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.55); }
      `}</style>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="field">
      <span className="fl">{label}</span>
      {children}
      <style jsx>{`
        .field { display: block; margin-bottom: 24px; }
        .fl {
          display: block;
          font-size: 9px;
          letter-spacing: 0.22em;
          color: rgba(255,255,255,0.45);
          font-weight: 700;
          margin-bottom: 10px;
        }
        .field :global(textarea),
        .field :global(input) {
          width: 100%;
          box-sizing: border-box;
          background: rgba(255, 255, 255, 0.03);
          color: #f5f5f7;
          border: 1px solid rgba(255, 255, 255, 0.09);
          border-radius: 0;
          padding: 12px 14px;
          font-family: "Fraunces", Georgia, serif;
          font-size: 16px;
          font-style: italic;
          font-weight: 300;
          line-height: 1.5;
          transition: border-color 0.18s, background 0.18s;
        }
        .field :global(input) {
          font-family: "JetBrains Mono", monospace;
          font-size: 13px;
          font-style: normal;
          font-weight: 400;
        }
        .field :global(textarea:focus),
        .field :global(input:focus) {
          outline: none;
          border-color: #b2fd02;
          background: rgba(178, 253, 2, 0.03);
        }
        .field :global(textarea::placeholder),
        .field :global(input::placeholder) {
          color: rgba(255, 255, 255, 0.25);
        }
        .field :global(.chips) {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
        }
        .field :global(.chip) {
          background: transparent;
          color: rgba(255, 255, 255, 0.55);
          border: 1px solid rgba(255, 255, 255, 0.12);
          padding: 7px 14px;
          font-family: "JetBrains Mono", monospace;
          font-size: 11px;
          font-weight: 500;
          letter-spacing: 0.05em;
          cursor: pointer;
          border-radius: 2px;
          transition: all 0.15s;
        }
        .field :global(.chip:hover) {
          color: #fff;
          border-color: rgba(255, 255, 255, 0.3);
        }
        .field :global(.chip.on) {
          background: #b2fd02;
          color: #000;
          border-color: #b2fd02;
          font-weight: 700;
        }
        .field :global(.chip.star) {
          letter-spacing: 0.1em;
          color: rgba(245, 158, 11, 0.6);
        }
        .field :global(.chip.star.on) {
          background: #f59e0b;
          color: #000;
          border-color: #f59e0b;
        }
        .field :global(.chip.warn.on) {
          background: #f59e0b;
          color: #000;
          border-color: #f59e0b;
        }
        .field :global(.chip.gchip) {
          font-family: "Fraunces", serif;
          font-size: 20px;
          font-weight: 700;
          padding: 6px 18px;
          letter-spacing: 0;
        }
        .field :global(.gchip.g-A.on) { background: #b2fd02; color: #000; border-color: #b2fd02; }
        .field :global(.gchip.g-B.on) { background: #9ca3af; color: #000; border-color: #9ca3af; }
        .field :global(.gchip.g-C.on) { background: #f59e0b; color: #000; border-color: #f59e0b; }
        .field :global(.gchip.g-D.on), .field :global(.gchip.g-F.on) { background: #ff4d4d; color: #000; border-color: #ff4d4d; }
      `}</style>
    </label>
  );
}
