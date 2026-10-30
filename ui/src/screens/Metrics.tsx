import { FlaskConical, Scale, ShieldCheck } from "lucide-react";
import { api, type Metrics as M } from "../api";
import { RateBars, StackedBars } from "../components/charts";
import { Empty, ErrorBanner, PageHead, ms, pct, useAsync } from "../components/common";

type Planted = {
  run: string;
  generated: string;
  catch_rate: number;
  pair_accuracy: number | null;
  severe_leak_rate: number | null;
  s1_overblocking: number | null;
  decision_rule: string;
  baseline: { severe_leak_rate: number | null; s1_overblocking: number | null; catch_rate: number };
  by_dimension: Record<string, number>;
};
type Fairness = {
  available: boolean;
  detail?: string;
  generated?: string;
  engine_groups?: number;
  engine_groups_with_differences?: number;
  judged_groups?: number;
  max_score_gap?: number | null;
  groups_with_approval_gap?: number;
};
type Consistency = { generated: string; replies: number; decision_flip_rate: number; by_tier: Record<string, number> };

export function Metrics() {
  const m = useAsync(() => api<M>("/ops/metrics/judge?window=all"), [], 30000);
  const fair = useAsync(() => api<Fairness>("/ops/metrics/fairness"), []);
  const q = (m.data?.quality_signals ?? {}) as { planted_errors?: Planted; judge_consistency?: Consistency };
  const p = q.planted_errors;
  const c = q.judge_consistency;
  return (
    <>
      <PageHead title="Metrics + fairness">
        Live totals from every stored judgement, next to the latest offline evaluation (planted errors on held-out cases, counterfactual fairness and
        judge consistency).
      </PageHead>
      <ErrorBanner error={m.error} />
      <div className="grid two">
        <div className="card">
          <div className="card-head">
            <h2>All judgements so far</h2>
            <span className="small muted">{m.data?.judgements ?? 0} total</span>
          </div>
          {m.data && Object.keys(m.data.decisions_by_tier).length ? (
            <>
              <StackedBars
                series={["approve", "revise", "handoff"]}
                rows={Object.entries(m.data.decisions_by_tier).map(([tier, d]) => ({
                  label: tier,
                  segments: ["approve", "revise", "handoff"].map((k) => ({ label: k, value: d[k] ?? 0 })),
                }))}
              />
              <div className="small ink2">
                Latency p50 {ms(m.data.latency_ms_p50)}, p95 {ms(m.data.latency_ms_p95)} · S3 panel disagreement {pct(m.data.panel_disagreement_rate)} ·
                models {Object.keys(m.data.models).map((x) => x.split("/").pop()).join(", ") || "none"}
              </div>
            </>
          ) : (
            <Empty>No judgements yet.</Empty>
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <h2 className="row">
              <FlaskConical size={16} aria-hidden /> Latest evaluation (planted errors)
            </h2>
            {p && <span className="small muted">{p.run} · {p.generated}</span>}
          </div>
          {p ? (
            <div className="stack">
              <table className="data">
                <thead>
                  <tr>
                    <th>Metric</th>
                    <th>Severity-aware</th>
                    <th>Fixed baseline</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Severe leak rate (S3 bad replies approved)</td>
                    <td className="tabular">{pct(p.severe_leak_rate)}</td>
                    <td className="tabular">{pct(p.baseline.severe_leak_rate)}</td>
                  </tr>
                  <tr>
                    <td>S1 over-blocking</td>
                    <td className="tabular">{pct(p.s1_overblocking)}</td>
                    <td className="tabular">{pct(p.baseline.s1_overblocking)}</td>
                  </tr>
                  <tr>
                    <td>Catch rate (all planted errors)</td>
                    <td className="tabular">{pct(p.catch_rate)}</td>
                    <td className="tabular">{pct(p.baseline.catch_rate)}</td>
                  </tr>
                  <tr>
                    <td>Controlled-pair accuracy</td>
                    <td className="tabular">{pct(p.pair_accuracy)}</td>
                    <td />
                  </tr>
                </tbody>
              </table>
              <div className="small">
                Pre-registered decision rule: <strong>{p.decision_rule}</strong>
              </div>
              <h3>Catch rate per dimension (severity-aware)</h3>
              <RateBars rows={Object.entries(p.by_dimension).map(([d, v]) => ({ label: d.replace("_", " "), value: v }))} target={0.95} />
              <div className="small muted">Tick: the 95 % target for must-pass dimensions.</div>
            </div>
          ) : (
            <Empty>No evaluation run yet (scripts/eval_run.py, then scripts/eval_report.py).</Empty>
          )}
        </div>
      </div>
      <div className="grid two" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head">
            <h2 className="row">
              <Scale size={16} aria-hidden /> Fairness (counterfactual passengers)
            </h2>
          </div>
          <ErrorBanner error={fair.error} />
          {fair.data?.available ? (
            <div className="stack">
              <div className="row">
                <ShieldCheck size={16} color={fair.data.engine_groups_with_differences ? "var(--critical)" : "var(--good)"} aria-hidden />
                <span>
                  Entitlements differ in <strong>{fair.data.engine_groups_with_differences}</strong> of {fair.data.engine_groups} groups (must be 0): the
                  same case gets the same euros whatever the passenger's name, English, tone or loyalty tier.
                </span>
              </div>
              <div className="small ink2">
                Reply quality across {fair.data.judged_groups} judged groups: largest gap in mean score {fair.data.max_score_gap?.toFixed(2) ?? "n/a"} (1-5
                scale); groups where one variant was approved and another not: {fair.data.groups_with_approval_gap}.
              </div>
            </div>
          ) : (
            <Empty>{fair.data?.detail ?? "Not computed yet."}</Empty>
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <h2>Judge consistency</h2>
            {c && <span className="small muted">{c.generated}</span>}
          </div>
          {c ? (
            <div className="stack">
              <div>
                Same reply judged three times with the cache off: the decision changed for <strong>{pct(c.decision_flip_rate)}</strong> of {c.replies}{" "}
                replies.
              </div>
              <RateBars rows={Object.entries(c.by_tier).map(([t, v]) => ({ label: t, value: v }))} />
              <div className="small muted">Share of replies whose decision changed between repeats, per tier.</div>
            </div>
          ) : (
            <Empty>Run scripts/judge_consistency.py to measure.</Empty>
          )}
        </div>
      </div>
    </>
  );
}
