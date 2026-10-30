import { CheckCircle2, Scale, Users, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import type { Diff, JudgementRecord, Score } from "../api";
import { DecisionChip, TierChip, ms } from "./common";

const LABEL: Record<string, string> = {
  relevance: "Relevance",
  context_awareness: "Context awareness",
  urgency: "Urgency first",
  tone: "Tone",
  grievance_handling: "Grievance handling",
  correctness: "Correctness",
  actionability: "Actionability",
  fairness_safety: "Fairness & safety",
};

/** Split the draft into plain and highlighted runs for every evidence quote found in it. */
function highlight(draft: string, scores: Score[]): { text: string; dims: string[] }[] {
  const spans: { start: number; end: number; dim: string }[] = [];
  for (const s of scores) {
    const q = s.evidence?.trim();
    if (!q) continue;
    const at = draft.indexOf(q);
    if (at >= 0) spans.push({ start: at, end: at + q.length, dim: s.id });
  }
  const cuts = new Set<number>([0, draft.length]);
  spans.forEach((s) => (cuts.add(s.start), cuts.add(s.end)));
  const points = [...cuts].sort((a, b) => a - b);
  const out: { text: string; dims: string[] }[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const [a, b] = [points[i], points[i + 1]];
    out.push({ text: draft.slice(a, b), dims: spans.filter((s) => s.start <= a && s.end >= b).map((s) => s.dim) });
  }
  return out;
}

export function Scorecard({ record, draft, minScores }: { record: JudgementRecord; draft?: string; minScores?: Record<string, number> }) {
  const [focus, setFocus] = useState<string | null>(null);
  const runs = useMemo(() => (draft ? highlight(draft, record.scores) : []), [draft, record.scores]);
  const failed = new Set(record.failures.map((f) => f.dimension));
  const panel = record.panel;
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="decision">
        <Scale size={18} aria-hidden />
        <div>
          <div className="row">
            <strong>{record.decision}</strong>
            <DecisionChip decision={record.decision} />
            <TierChip tier={record.severity_tier} />
            <span className="chip">verdict: {record.verdict}</span>
          </div>
          <div className="small ink2">
            Decided by code: {record.decision_reason}
            {record.failures.length > 0 && <> · fails: {record.failures.map((f) => `${f.dimension} (${f.reason})`).join("; ")}</>}
          </div>
        </div>
      </div>
      {record.error && <div className="banner error">Judge error: {record.error}</div>}
      {record.scores.length === 0 ? (
        <div className="small muted">No model scores: a code check failed, so the judge was not called.</div>
      ) : (
        <div className="dims" role="table" aria-label="Dimension scores">
          {record.scores.map((s) => {
            const bar = minScores?.[s.id];
            const isFail = failed.has(s.id);
            return (
              <div key={s.id} style={{ display: "contents" }} onMouseEnter={() => setFocus(s.id)} onMouseLeave={() => setFocus(null)}>
                <span className="name" style={{ color: isFail ? "var(--critical)" : undefined }}>
                  {LABEL[s.id] ?? s.id}
                </span>
                {s.kind === "scale" ? (
                  <div className="bar" title={`${s.score} / 5${bar ? `, tier bar ${bar}` : ""}`}>
                    <span style={{ width: `${((s.score ?? 0) / 5) * 100}%`, background: isFail ? "var(--critical)" : undefined }} />
                    {bar && <i style={{ left: `${(bar / 5) * 100}%` }} aria-hidden />}
                  </div>
                ) : (
                  <span className="row small">
                    {s.passed ? <CheckCircle2 size={15} color="var(--good)" aria-hidden /> : <XCircle size={15} color="var(--critical)" aria-hidden />}
                    {s.passed ? "pass" : "fail"} (must pass)
                  </span>
                )}
                <span className="tabular right small">{s.kind === "scale" ? `${s.score ?? "–"} / 5` : ""}</span>
                <span className="why">
                  {s.rationale}
                  {s.evidence && (
                    <>
                      {" "}
                      <q className="ink2">{s.evidence}</q>
                      {s.evidence_ok === false && <span className="chip bad">quote not found in draft</span>}
                    </>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}
      {draft && (
        <div>
          <h3 style={{ marginBottom: 6 }}>Draft, with the judge's evidence highlighted</h3>
          <div className="draft">
            {runs.map((r, i) =>
              r.dims.length ? (
                <mark key={i} className={focus && r.dims.includes(focus) ? "active" : undefined} title={r.dims.map((d) => LABEL[d] ?? d).join(", ")}>
                  {r.text}
                </mark>
              ) : (
                <span key={i}>{r.text}</span>
              ),
            )}
          </div>
          <div className="small muted" style={{ marginTop: 4 }}>Hover a dimension to see which quote supports it.</div>
        </div>
      )}
      {record.analysis && (
        <div>
          <h3>Judge's analysis</h3>
          <p className="ink2" style={{ margin: "4px 0 0" }}>{record.analysis}</p>
        </div>
      )}
      {panel && (
        <div>
          <h3 className="row"><Users size={15} aria-hidden /> Panel (S3)</h3>
          <div className="row small" style={{ marginTop: 6 }}>
            {panel.members.map((m, i) => (
              <span key={m} className={`chip ${panel.member_passed[i] ? "good" : "bad"}`}>
                {m}: {panel.member_passed[i] ? "passes" : "fails"}
              </span>
            ))}
            {panel.disagreement && <span className="chip bad">disagreement → handoff</span>}
          </div>
        </div>
      )}
      <div className="small muted">
        {record.rubric_id} · {record.prompt_version} · exemplars {record.exemplar_count ?? 0} · {ms(record.latency_ms)} ·{" "}
        {Object.keys(record.models ?? {}).join(", ") || "no model call"}
      </div>
    </div>
  );
}

/** Word-level diff (LCS) between the failed draft and its revision. */
function wordDiff(a: string, b: string): { op: "=" | "-" | "+"; text: string }[] {
  const A = a.split(/(\s+)/);
  const B = b.split(/(\s+)/);
  const n = A.length, m = B.length;
  if (n * m > 400_000) return [{ op: "-", text: a }, { op: "+", text: b }];
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: { op: "=" | "-" | "+"; text: string }[] = [];
  let i = 0, j = 0;
  const push = (op: "=" | "-" | "+", text: string) => {
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += text;
    else out.push({ op, text });
  };
  while (i < n && j < m) {
    if (A[i] === B[j]) push("=", A[i++]), j++;
    else if (dp[i + 1][j] >= dp[i][j + 1]) push("-", A[i++]);
    else push("+", B[j++]);
  }
  while (i < n) push("-", A[i++]);
  while (j < m) push("+", B[j++]);
  return out;
}

export function DiffView({ diff }: { diff: Diff }) {
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div>
        <h3>Guidance sent back to the agent</h3>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {diff.failed_checks.map((c) => (
            <li key={c.check_id}>
              <strong>{c.check_id}</strong>: {c.detail} <span className="chip bad">code check</span>
            </li>
          ))}
          {diff.guidance.map((g) => (
            <li key={g}>{g}</li>
          ))}
        </ul>
      </div>
      {diff.revision ? (
        <div>
          <h3 style={{ marginBottom: 6 }}>Failed draft → revision</h3>
          <div className="draft diff">
            {wordDiff(diff.failed_draft, diff.revision).map((p, k) =>
              p.op === "=" ? <span key={k}>{p.text}</span> : p.op === "-" ? <del key={k}>{p.text}</del> : <ins key={k}>{p.text}</ins>,
            )}
          </div>
          <div className="legend small" style={{ marginTop: 6 }}>
            <span><del>removed</del></span>
            <span><ins>added</ins></span>
          </div>
        </div>
      ) : (
        <div>
          <h3 style={{ marginBottom: 6 }}>Failed draft</h3>
          <div className="draft">{diff.failed_draft}</div>
          <div className="small muted" style={{ marginTop: 4 }}>No revision followed (approved first time, or handed off).</div>
        </div>
      )}
    </div>
  );
}
