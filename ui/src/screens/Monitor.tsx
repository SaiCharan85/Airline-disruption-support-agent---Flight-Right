import { Activity, AlertTriangle, Gauge, Hourglass, Pause, Play, Users } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { Open } from "../App";
import { api, stream, type Judgement, type Metrics, type Notification, type StreamEvent } from "../api";
import { RateBars, StackedBars, TrendLine } from "../components/charts";
import { DecisionChip, Empty, ErrorBanner, PageHead, TierChip, ms, pct, time, useAsync } from "../components/common";

type FeedRow = {
  judgement_id: string;
  conversation_id?: string;
  tier?: string;
  decision?: string;
  verdict?: string;
  failed: string[];
  evidence?: string;
  latency_ms?: number;
  models: string[];
  attempt?: number;
  ts: string;
  fresh?: boolean;
};

const WINDOWS = ["15m", "1h", "24h", "7d"] as const;
const DECISIONS = ["approve", "revise", "handoff"];

function fromStored(j: Judgement): FeedRow {
  return {
    judgement_id: j.judgement_id,
    conversation_id: j.conversation_id,
    tier: j.tier,
    decision: j.decision,
    verdict: j.verdict,
    failed: [...new Set(j.record.failures.map((f) => f.dimension).filter((d) => d !== "*"))],
    evidence: j.record.scores.find((s) => s.evidence)?.evidence,
    latency_ms: j.record.latency_ms,
    models: Object.keys(j.record.models ?? {}),
    ts: j.created_at,
  };
}

function fromEvent(e: StreamEvent): FeedRow | null {
  const p = e.payload as Record<string, unknown>;
  if (!p.judgement_id) return null;
  return {
    judgement_id: String(p.judgement_id),
    conversation_id: e.conversation_id,
    tier: p.tier as string,
    decision: (p.judge_decision as string) ?? (p.decision as string),
    verdict: p.verdict as string,
    failed: (p.failed_dimensions as string[]) ?? [],
    evidence: p.evidence as string,
    latency_ms: p.latency_ms as number,
    models: (p.models as string[]) ?? [],
    attempt: e.attempt_no,
    ts: e.ts,
    fresh: true,
  };
}

export function Monitor({ open }: { open: Open }) {
  const [win, setWin] = useState<(typeof WINDOWS)[number]>("1h");
  const metrics = useAsync(() => api<Metrics>(`/ops/metrics/judge?window=${win}`), [win], 15000);
  const recent = useAsync(() => api<{ items: Judgement[] }>("/ops/judgements?limit=200"), [], 60000);
  const alerts = useAsync(() => api<Notification[]>("/ops/notifications?kind=alert"), [], 30000);
  const [live, setLive] = useState<FeedRow[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState({ tier: "", decision: "", dimension: "" });

  useEffect(() => {
    if (paused) return;
    // The stream replays stored judgements first, then pushes new ones as they happen.
    return stream("/ops/judgements/stream", (e) => {
      const row = fromEvent(e);
      if (row) setLive((xs) => (xs.some((x) => x.judgement_id === row.judgement_id) ? xs : [row, ...xs].slice(0, 300)));
    });
  }, [paused]);

  const feed = useMemo(() => {
    const seen = new Set(live.map((r) => r.judgement_id));
    const stored = (recent.data?.items ?? []).map(fromStored).filter((r) => !seen.has(r.judgement_id));
    return [...live, ...stored]
      .filter((r) => (!filter.tier || r.tier === filter.tier) && (!filter.decision || r.decision === filter.decision))
      .filter((r) => !filter.dimension || r.failed.includes(filter.dimension))
      .sort((a, b) => (a.ts < b.ts ? 1 : -1));
  }, [live, recent.data, filter]);

  // Judgements per 10-minute bucket, for the trend line (display aggregation only).
  const trend = useMemo(() => {
    const items = recent.data?.items ?? [];
    const buckets = new Map<number, number>();
    for (const j of items) {
      const t = Math.floor(new Date(j.created_at).getTime() / 600000) * 600000;
      buckets.set(t, (buckets.get(t) ?? 0) + 1);
    }
    return [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ t, v }));
  }, [recent.data]);

  const m = metrics.data;
  const q = (m?.quality_signals ?? {}) as Record<string, any>;
  const dims = Object.keys(m?.dimension_pass_rate ?? {});
  return (
    <>
      <PageHead
        title="Judge Monitor"
        actions={
          <div className="row" role="group" aria-label="Window">
            {WINDOWS.map((w) => (
              <button key={w} className={`btn ${w === win ? "primary" : ""}`} onClick={() => setWin(w)} aria-pressed={w === win}>
                last {w}
              </button>
            ))}
          </div>
        }
      >
        Every verdict across all conversations as it happens, and how the judge is behaving over the selected window. All numbers come from the API.
      </PageHead>
      <ErrorBanner error={metrics.error} />
      <div className="grid tiles">
        <Tile icon={<Activity size={14} />} label="Judgements" value={String(m?.judgements ?? "–")} sub={`last ${win}`} />
        <Tile icon={<Users size={14} />} label="S3 panel disagreement" value={pct(m?.panel_disagreement_rate)} sub="share of S3 panels split → handoff" />
        <Tile icon={<AlertTriangle size={14} />} label="No trustworthy verdict" value={pct(m?.judge_unavailable_rate)} sub="model failed or quotes invented" />
        <Tile icon={<Hourglass size={14} />} label="Judge latency" value={ms(m?.latency_ms_p50)} sub={`p95 ${ms(m?.latency_ms_p95)}`} />
        <Tile
          icon={<Gauge size={14} />}
          label="Planted-error catch rate"
          value={q.planted_errors?.catch_rate !== undefined ? pct(q.planted_errors.catch_rate) : "n/a"}
          sub="latest eval run"
        />
        <Tile
          icon={<Gauge size={14} />}
          label="Reviewer agreement"
          value={pct(q.reviewer_agreement as number | undefined)}
          sub={`${q.corrections ?? 0} workbench corrections`}
        />
      </div>
      <div className="grid two" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head">
            <h2>Decisions per tier</h2>
          </div>
          {m && Object.keys(m.decisions_by_tier).length ? (
            <StackedBars
              series={DECISIONS}
              rows={Object.entries(m.decisions_by_tier).map(([tier, d]) => ({
                label: tier,
                segments: DECISIONS.map((k) => ({ label: k, value: d[k] ?? 0 })),
              }))}
            />
          ) : (
            <Empty>No judgements in this window.</Empty>
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <h2>Pass rate per dimension</h2>
          </div>
          {dims.length ? (
            <RateBars rows={dims.map((d) => ({ label: d.replace("_", " "), value: m!.dimension_pass_rate[d] }))} />
          ) : (
            <Empty>No scored judgements in this window.</Empty>
          )}
        </div>
      </div>
      <div className="grid two" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head">
            <h2>Judgements per 10 minutes</h2>
            <span className="small muted">last 200</span>
          </div>
          <TrendLine points={trend} label="Judgements per 10 minutes" />
        </div>
        <div className="card">
          <div className="card-head">
            <h2>Free-tier usage today</h2>
          </div>
          {m && Object.keys(m.quota_today).length ? (
            <RateBars
              rows={Object.entries(m.quota_today).map(([k, v]) => ({
                label: k.split(":")[1] ?? k,
                value: v.rpd_limit ? v.requests / v.rpd_limit : null,
              }))}
              target={0.9}
            />
          ) : (
            <Empty>No model calls today.</Empty>
          )}
          <div className="small muted" style={{ marginTop: 6 }}>Share of each model's daily request limit; the tick is the 90 % hard stop.</div>
        </div>
      </div>
      <div className="grid chat" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head">
            <h2>Live feed</h2>
            <div className="row">
              <select value={filter.tier} onChange={(e) => setFilter({ ...filter, tier: e.target.value })} aria-label="Tier filter">
                <option value="">all tiers</option>
                {["S1", "S2", "S3", "S4"].map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
              <select value={filter.decision} onChange={(e) => setFilter({ ...filter, decision: e.target.value })} aria-label="Decision filter">
                <option value="">all decisions</option>
                {DECISIONS.map((d) => (
                  <option key={d}>{d}</option>
                ))}
              </select>
              <select value={filter.dimension} onChange={(e) => setFilter({ ...filter, dimension: e.target.value })} aria-label="Failed dimension filter">
                <option value="">any dimension</option>
                {dims.map((d) => (
                  <option key={d}>{d}</option>
                ))}
              </select>
              <button className="btn" onClick={() => setPaused(!paused)} aria-pressed={paused}>
                {paused ? <Play size={14} /> : <Pause size={14} />} {paused ? "Resume" : "Pause"}
              </button>
            </div>
          </div>
          {feed.length === 0 ? (
            <Empty>No judgements yet. Start a conversation in Chat.</Empty>
          ) : (
            <div className="feed" role="list">
              {feed.slice(0, 150).map((r) => (
                <div
                  key={r.judgement_id}
                  role="listitem"
                  tabIndex={0}
                  className={`feed-row ${r.fresh ? "enter" : ""}`}
                  onClick={() => open(r.judgement_id)}
                  onKeyDown={(e) => e.key === "Enter" && open(r.judgement_id)}
                >
                  <span className="small muted tabular">{time(r.ts)}</span>
                  <TierChip tier={r.tier} />
                  <DecisionChip decision={r.decision} />
                  <span className="small" style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.failed.length ? <strong>fails {r.failed.join(", ")}</strong> : <span className="ink2">all bars met</span>}
                    {r.evidence && <span className="muted"> · “{r.evidence}”</span>}
                  </span>
                  <span className="small muted hide-sm">
                    {ms(r.latency_ms)} · {r.models[0]?.split("/").pop() ?? "code"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <h2>Alert history</h2>
          </div>
          {alerts.data?.length ? (
            <div className="stack">
              {alerts.data.slice(0, 30).map((a) => (
                <div key={a.id} className="banner">
                  <AlertTriangle size={15} color="var(--serious)" aria-hidden />
                  <span>
                    <span className="small muted">{new Date(a.created_at).toLocaleString()}</span>
                    <br />
                    {a.text}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>No alerts. n8n posts threshold breaches here (W4).</Empty>
          )}
        </div>
      </div>
    </>
  );
}

function Tile({ icon, label, value, sub }: { icon: React.ReactNode; label: string; value: string; sub: string }) {
  return (
    <div className="card tile">
      <div className="label">
        {icon}
        {label}
      </div>
      <div className="value tabular">{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}
