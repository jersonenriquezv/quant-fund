"use client";

import { useEffect, useState, useCallback, use } from "react";
import Link from "next/link";
import { fetchApi, patchApi } from "@/lib/api";
import type { BybitAnnotation, BybitAnnotationPatch } from "@/lib/api";

const EMOTIONAL_STATES = ["calm", "confident", "FOMO", "revenge", "tired", "uncertain"];

const GRADE_COLORS: Record<string, string> = {
  A: "#b2fd02",
  B: "#9ca3af",
  C: "#f59e0b",
  D: "#ff4d4d",
};

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

type ParamsP = Promise<{ id: string }>;

export default function AnnotatePage({ params }: { params: ParamsP }) {
  const { id } = use(params);
  const annotationId = Number(id);
  const [annot, setAnnot] = useState<BybitAnnotation | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string>("");

  const [thesis, setThesis] = useState("");
  const [lesson, setLesson] = useState("");
  const [emotional, setEmotional] = useState("");
  const [screenshot, setScreenshot] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const a = await fetchApi<BybitAnnotation>(`/bybit/annotations/${annotationId}`);
      setAnnot(a);
      setThesis(a.thesis_pre || "");
      setLesson(a.lesson_post || "");
      setEmotional(a.emotional_state || "");
      setScreenshot(a.screenshot_url || "");
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
      const payload: BybitAnnotationPatch = {
        thesis_pre: thesis || null,
        lesson_post: lesson || null,
        emotional_state: emotional || null,
        screenshot_url: screenshot || null,
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
          <div className="ctx-eyebrow">AUTO CLASSIFICATION · v{annot.auto_classifier_version ?? "?"}</div>
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
            </div>
          </div>
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
        </section>
      )}

      <main className="form">
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

        <Field label="THESIS / WHY I ENTERED (OPTIONAL)">
          <textarea
            rows={4}
            value={thesis}
            onChange={(e) => setThesis(e.target.value)}
            placeholder="lo que vi, por qué tomé el trade…"
          />
        </Field>

        {isClosed && (
          <Field label="LESSON / WHAT I'D DO DIFFERENT">
            <textarea
              rows={4}
              value={lesson}
              onChange={(e) => setLesson(e.target.value)}
              placeholder="post-mortem honesto…"
            />
          </Field>
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

        .form {
          max-width: 720px;
          margin: 0 auto;
          padding: 10px 24px 80px 24px;
          animation: fade 0.8s 0.15s ease both;
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
