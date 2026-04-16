"use client";

import { useEffect, useState, useCallback, use } from "react";
import Link from "next/link";
import { fetchApi, patchApi } from "@/lib/api";
import type { BybitPendingOrder, BybitPendingPatch } from "@/lib/api";

const SETUP_TYPES = [
  "A_swing_long", "A_swing_short", "B_sweep", "C_continuation",
  "D_choch", "D_bos", "F_breakout", "discretion", "news_play", "other",
];
const CONFLUENCE_OPTIONS = [
  "OB_4H", "OB_1H", "OB_15m", "FVG", "sweep", "CHoCH", "BOS",
  "RSI_divergence", "volume_absorption", "liq_cluster_magnet",
  "value_area_bounce", "funding_extreme", "OI_divergence",
  "CVD_divergence", "htf_aligned", "news_catalyst",
];
const EMOTIONAL_STATES = ["calm", "confident", "FOMO", "revenge", "tired", "uncertain"];

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

type ParamsP = Promise<{ id: string }>;

export default function PendingPage({ params }: { params: ParamsP }) {
  const { id } = use(params);
  const pid = Number(id);
  const [p, setP] = useState<BybitPendingOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  const [setupType, setSetupType] = useState("");
  const [confluences, setConfluences] = useState<Set<string>>(new Set());
  const [confidence, setConfidence] = useState<number | null>(null);
  const [thesis, setThesis] = useState("");
  const [emotional, setEmotional] = useState("");
  const [screenshot, setScreenshot] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const a = await fetchApi<BybitPendingOrder>(`/bybit/pending/${pid}`);
      setP(a);
      setSetupType(a.setup_type || "");
      setConfluences(new Set(a.confluences || []));
      setConfidence(a.confidence);
      setThesis(a.thesis_pre || "");
      setEmotional(a.emotional_state || "");
      setScreenshot(a.screenshot_url || "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, [pid]);

  useEffect(() => { load(); }, [load]);

  const toggleConfluence = (c: string) => {
    setConfluences((prev) => {
      const next = new Set(prev);
      if (next.has(c)) next.delete(c); else next.add(c);
      return next;
    });
  };

  const save = async () => {
    setSaving(true); setSaved(false); setError("");
    try {
      const payload: BybitPendingPatch = {
        setup_type: setupType || null,
        confluences: confluences.size ? Array.from(confluences) : null,
        confidence,
        thesis_pre: thesis || null,
        emotional_state: emotional || null,
        screenshot_url: screenshot || null,
      };
      const u = await patchApi<BybitPendingOrder>(`/bybit/pending/${pid}`, payload);
      setP(u);
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
  if (!p) {
    return <div style={{ padding: 40, color: "#ff4d4d", fontFamily: "JetBrains Mono" }}>{error || "not found"}</div>;
  }

  const ctx = p.context_snapshot as Record<string, unknown> | null;
  const htf = (ctx?.htf_bias as Record<string, unknown> | undefined) || {};
  const warnings = (ctx?.warnings as string[] | undefined) || [];
  const isLong = p.side === "Buy";
  const displayType = p.stop_order_type || p.order_type || "LIMIT";
  const priceStr = p.price ? fmt(p.price, 4) : p.trigger_price ? `trig ${fmt(p.trigger_price, 4)}` : "—";
  const terminal = p.status !== "pending";

  return (
    <div className="annot-root">
      <div className="grain" />

      <header className="a-head">
        <div className="breadcrumb">
          <Link href="/bybit" className="back">← LOG</Link>
          <span className="crumb-sep">/</span>
          <span className="crumb-id">PENDING #{p.id}</span>
        </div>
        <div className="identity">
          <span className={`dir ${isLong ? "long" : "short"}`}>{isLong ? "LONG" : "SHORT"}</span>
          <h1 className="sym">{p.symbol}</h1>
          <span className={`status ${terminal ? (p.status === "filled" ? "filled" : "cancelled") : "pending"}`}>
            {terminal ? p.status.toUpperCase() : "◌ PENDING"}
          </span>
        </div>
        <div className="ticker">
          <TickerItem label="TYPE" value={displayType} />
          <TickerItem label="QTY" value={fmt(p.qty, 4)} />
          <TickerItem label="PRICE" value={priceStr} />
          {p.placed_to_fill_sec != null && <TickerItem label="→FILL" value={`${p.placed_to_fill_sec}s`} />}
          {p.placed_to_cancel_sec != null && <TickerItem label="→CNCL" value={`${p.placed_to_cancel_sec}s`} />}
        </div>
        {p.annotation_id && (
          <div className="linked">
            <Link href={`/annotate/${p.annotation_id}`} className="link-chip">
              → linked trade #{p.annotation_id}
            </Link>
          </div>
        )}
      </header>

      {ctx && (
        <section className="ctx-block">
          <div className="ctx-eyebrow">CONTEXT AT PLACEMENT</div>
          <ul className="ctx-list">
            <li><span className="cl">4H</span><span className="cv">{(htf.bias_4h as string) || "?"}</span></li>
            <li><span className="cl">1H</span><span className="cv">{(htf.bias_1h as string) || "?"}</span></li>
            {htf.aligned_with_trade === true && <li className="ok">· HTF aligned</li>}
            {htf.aligned_with_trade === false && <li className="warn">· HTF counter</li>}
            {ctx.funding != null && <li><span className="cl">FUND</span><span className="cv">{fmt(ctx.funding as number, 4)}%</span></li>}
            {ctx.oi_delta_1h_pct != null && <li><span className="cl">OI-1h</span><span className="cv">{fmt(ctx.oi_delta_1h_pct as number, 2)}%</span></li>}
          </ul>
          {warnings.length > 0 && (
            <div className="warn-row">
              {warnings.map((w, i) => <span key={i} className="warn-flag">⚠ {w}</span>)}
            </div>
          )}
        </section>
      )}

      <main className="form">
        <div className="pre-hint">
          <span className="serif italic">Anota tu thesis ANTES del fill.</span>
          <span className="sub">Si la orden ejecuta, esto se migra automáticamente al trade.</span>
        </div>

        <Field label="SETUP">
          <div className="chips">
            <button type="button" className={`chip ${!setupType ? "on" : ""}`} onClick={() => setSetupType("")}>none</button>
            {SETUP_TYPES.map((s) => (
              <button type="button" key={s} className={`chip ${setupType === s ? "on" : ""}`} onClick={() => setSetupType(s)}>{s}</button>
            ))}
          </div>
        </Field>

        <Field label="CONFLUENCES">
          <div className="chips">
            {CONFLUENCE_OPTIONS.map((c) => (
              <button type="button" key={c} className={`chip ${confluences.has(c) ? "on" : ""}`} onClick={() => toggleConfluence(c)}>{c}</button>
            ))}
          </div>
        </Field>

        <Field label="CONFIDENCE">
          <div className="chips">
            {[1, 2, 3, 4, 5].map((n) => (
              <button type="button" key={n} className={`chip star ${confidence === n ? "on" : ""}`} onClick={() => setConfidence(n)}>
                {"★".repeat(n)}{"☆".repeat(5 - n)}
              </button>
            ))}
          </div>
        </Field>

        <Field label="EMOTIONAL STATE">
          <div className="chips">
            {EMOTIONAL_STATES.map((e) => (
              <button type="button" key={e} className={`chip ${emotional === e ? "on" : ""}`} onClick={() => setEmotional(e)}>{e}</button>
            ))}
          </div>
        </Field>

        <Field label="THESIS / WHY THIS ORDER">
          <textarea rows={4} value={thesis} onChange={(e) => setThesis(e.target.value)} placeholder="what I'm waiting for, invalidation, targets…" />
        </Field>

        <Field label="SCREENSHOT URL (OPTIONAL)">
          <input type="url" value={screenshot} onChange={(e) => setScreenshot(e.target.value)} placeholder="https://tradingview.com/x/…" />
        </Field>

        <div className="save-row">
          <button className={`save ${saved ? "done" : ""}`} onClick={save} disabled={saving}>
            {saving ? "SAVING…" : saved ? "✓ SAVED" : "SAVE THESIS →"}
          </button>
          {error && <p className="err">ERROR · {error}</p>}
        </div>
      </main>

      <style jsx global>{`
        .annot-root { min-height: 100vh; background: #050505; color: #f5f5f7; font-family: "JetBrains Mono", monospace; position: relative; overflow-x: hidden; }
        .annot-root .serif { font-family: "Fraunces", Georgia, serif; }
        .annot-root .serif.italic { font-style: italic; }
        .annot-root .grain {
          position: fixed; inset: 0; pointer-events: none; z-index: 100; opacity: 0.035;
          background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
        }
      `}</style>
      <style jsx>{`
        .a-head { padding: 32px 24px 20px 24px; max-width: 720px; margin: 0 auto; animation: fade 0.6s ease both; }
        .breadcrumb { display: flex; gap: 10px; align-items: center; font-size: 11px; letter-spacing: 0.15em; color: rgba(255,255,255,0.4); margin-bottom: 24px; }
        .back { color: rgba(255,255,255,0.6); text-decoration: none; font-weight: 600; transition: color 0.2s; }
        .back:hover { color: #b2fd02; }
        .crumb-sep { color: rgba(255,255,255,0.25); }
        .identity { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
        .dir { font-size: 10px; font-weight: 700; letter-spacing: 0.2em; padding: 4px 10px; border-radius: 2px; }
        .dir.long { color: #b2fd02; background: rgba(178,253,2,0.1); }
        .dir.short { color: #ff4d4d; background: rgba(255,77,77,0.1); }
        .sym { font-family: "Instrument Serif", Georgia, serif; font-size: clamp(36px, 7vw, 64px); font-weight: 400; letter-spacing: -0.02em; line-height: 1; margin: 0; color: #f5f5f7; }
        .status { font-size: 10px; letter-spacing: 0.18em; font-weight: 700; padding: 3px 9px; border-radius: 2px; }
        .status.pending { color: #f59e0b; background: rgba(245,158,11,0.08); animation: pulse 1.6s ease-in-out infinite; }
        .status.filled { color: #b2fd02; background: rgba(178,253,2,0.08); }
        .status.cancelled { color: rgba(255,255,255,0.5); background: rgba(255,255,255,0.06); }
        @keyframes pulse { 0%,100% { opacity: 0.7; } 50% { opacity: 1; } }
        .ticker { margin-top: 22px; display: flex; gap: 0; flex-wrap: wrap; border-top: 1px solid rgba(255, 255, 255, 0.08); border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
        .linked { margin-top: 14px; }
        .link-chip { display: inline-block; padding: 6px 12px; border: 1px solid #b2fd02; color: #b2fd02; font-size: 11px; letter-spacing: 0.1em; text-decoration: none; border-radius: 2px; }
        .link-chip:hover { background: rgba(178,253,2,0.08); }

        .ctx-block { max-width: 720px; margin: 0 auto; padding: 24px; }
        .ctx-eyebrow { font-size: 9px; letter-spacing: 0.24em; color: rgba(255,255,255,0.35); font-weight: 600; margin-bottom: 12px; }
        .ctx-list { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 16px 22px; font-size: 12px; }
        .ctx-list li { display: inline-flex; gap: 8px; align-items: baseline; }
        .cl { font-size: 9px; letter-spacing: 0.15em; color: rgba(255,255,255,0.4); }
        .cv { font-weight: 500; color: rgba(255,255,255,0.85); font-family: "JetBrains Mono", monospace; }
        .ctx-list li.ok { color: #b2fd02; }
        .ctx-list li.warn { color: #f59e0b; }
        .warn-row { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 6px; }
        .warn-flag { font-size: 11px; color: #f59e0b; background: rgba(245,158,11,0.08); padding: 3px 9px; border-radius: 2px; }

        .form { max-width: 720px; margin: 0 auto; padding: 10px 24px 80px 24px; }
        .pre-hint { padding: 16px; background: rgba(245, 158, 11, 0.05); border-left: 2px solid #f59e0b; margin-bottom: 24px; display: flex; flex-direction: column; gap: 4px; }
        .pre-hint :global(.serif.italic) { font-size: 17px; color: #f5f5f7; }
        .pre-hint .sub { font-size: 11px; color: rgba(255,255,255,0.5); letter-spacing: 0.06em; }
        .save-row { margin-top: 28px; }
        .save { width: 100%; padding: 18px; background: #f59e0b; color: #000; border: none; font-family: "JetBrains Mono", monospace; font-weight: 700; font-size: 13px; letter-spacing: 0.16em; cursor: pointer; border-radius: 0; transition: all 0.18s; }
        .save:hover { background: #fbbf24; transform: translateY(-1px); }
        .save:disabled { opacity: 0.6; cursor: wait; }
        .save.done { background: transparent; color: #b2fd02; border: 1px solid #b2fd02; }
        .err { margin-top: 12px; color: #ff4d4d; font-size: 11px; letter-spacing: 0.12em; }
        @keyframes fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
        @media (max-width: 640px) { .a-head { padding: 24px 16px 16px; } .ctx-block, .form { padding-left: 16px; padding-right: 16px; } }
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
        .tk { flex: 1 1 100px; padding: 14px 18px; border-right: 1px solid rgba(255,255,255,0.08); min-width: 100px; }
        .tk:last-child { border-right: none; }
        .tk-l { font-size: 9px; letter-spacing: 0.2em; color: rgba(255,255,255,0.35); margin-bottom: 6px; }
        .tk-v { font-family: "Instrument Serif", Georgia, serif; font-size: 20px; font-weight: 400; letter-spacing: -0.01em; }
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
        .fl { display: block; font-size: 9px; letter-spacing: 0.22em; color: rgba(255,255,255,0.45); font-weight: 700; margin-bottom: 10px; }
        .field :global(textarea),
        .field :global(input) { width: 100%; box-sizing: border-box; background: rgba(255,255,255,0.03); color: #f5f5f7; border: 1px solid rgba(255,255,255,0.09); border-radius: 0; padding: 12px 14px; font-family: "Fraunces", Georgia, serif; font-size: 16px; font-style: italic; font-weight: 300; line-height: 1.5; transition: border-color 0.18s, background 0.18s; }
        .field :global(input) { font-family: "JetBrains Mono", monospace; font-size: 13px; font-style: normal; font-weight: 400; }
        .field :global(textarea:focus), .field :global(input:focus) { outline: none; border-color: #f59e0b; background: rgba(245,158,11,0.03); }
        .field :global(textarea::placeholder), .field :global(input::placeholder) { color: rgba(255,255,255,0.25); }
        .field :global(.chips) { display: flex; flex-wrap: wrap; gap: 6px; }
        .field :global(.chip) { background: transparent; color: rgba(255,255,255,0.55); border: 1px solid rgba(255,255,255,0.12); padding: 7px 14px; font-family: "JetBrains Mono", monospace; font-size: 11px; font-weight: 500; letter-spacing: 0.05em; cursor: pointer; border-radius: 2px; transition: all 0.15s; }
        .field :global(.chip:hover) { color: #fff; border-color: rgba(255,255,255,0.3); }
        .field :global(.chip.on) { background: #f59e0b; color: #000; border-color: #f59e0b; font-weight: 700; }
        .field :global(.chip.star) { letter-spacing: 0.1em; color: rgba(245, 158, 11, 0.6); }
        .field :global(.chip.star.on) { background: #f59e0b; color: #000; border-color: #f59e0b; }
      `}</style>
    </label>
  );
}
