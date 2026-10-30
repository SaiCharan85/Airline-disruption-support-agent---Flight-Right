import { BookOpen, GitPullRequestArrow, ListChecks, MessageSquarePlus, Sparkles } from "lucide-react";
import { useState } from "react";
import type { Open } from "../App";
import { api, post, type Judgement } from "../api";
import { RateBars } from "../components/charts";
import { DecisionChip, Empty, ErrorBanner, PageHead, TierChip, pct, useAsync } from "../components/common";

type Correction = {
  correction_id: string;
  judgement_id: string;
  split: string;
  created_at: string;
  record: {
    tier: string;
    rubric_id?: string;
    reason: string;
    dimension_overrides: Record<string, number | boolean>;
    decision_override: string | null;
    verdict_override: string | null;
    original_scores: Record<string, number | boolean>;
    original_decision: string;
    agrees_with_decision: boolean;
    excerpt: string;
  };
};
type Example = { example_id: string; created_at: string; record: { prompt_text: string; expected_reply: string; notes: string; split: string } };
type Proposal = { proposal_id: string; dimension: string; status: string; created_at: string; change: { tier: string; action: string; evidence: string } };

type Tab = "review" | "corrections" | "examples" | "proposals";

export function Workbench({ open, initial }: { open: Open; initial?: string }) {
  const [tab, setTab] = useState<Tab>((initial as Tab) || "review");
  return (
    <>
      <PageHead title="Judge Workbench">
        See why the judge decided, correct it in one click, and teach it with examples. Corrections become few-shot examples in the judge's prompt
        (chosen by tier and dimension, most recent first), and consistent corrections turn into a proposed bar change that applies only when you approve it.
        The model is never retrained, and nothing from the held-out test split is accepted.
      </PageHead>
      <div className="tabs" role="tablist">
        {(
          [
            ["review", "Review", <ListChecks size={15} key="r" />],
            ["corrections", "Corrections + agreement", <Sparkles size={15} key="c" />],
            ["examples", "Expected replies", <MessageSquarePlus size={15} key="e" />],
            ["proposals", "Proposed changes", <GitPullRequestArrow size={15} key="p" />],
          ] as [Tab, string, React.ReactNode][]
        ).map(([id, label, icon]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
            <span className="row">
              {icon}
              {label}
            </span>
          </button>
        ))}
      </div>
      {tab === "review" && <Review open={open} />}
      {tab === "corrections" && <Corrections open={open} />}
      {tab === "examples" && <Examples />}
      {tab === "proposals" && <Proposals />}
    </>
  );
}

function Review({ open }: { open: Open }) {
  const [onlyHard, setOnlyHard] = useState(true);
  const res = useAsync(() => api<{ items: Judgement[] }>("/ops/judgements?limit=100"), [], 20000);
  const corr = useAsync(() => api<Correction[]>("/ops/judge/corrections"), []);
  const done = new Set((corr.data ?? []).map((c) => c.judgement_id));
  const items = (res.data?.items ?? [])
    .filter((j) => j.record.scores.length > 0)
    .filter((j) => !onlyHard || j.decision !== "approve" || j.record.panel?.disagreement || j.tier === "S3");
  return (
    <div className="card">
      <div className="card-head">
        <h2>Judgements to look at</h2>
        <label className="row small">
          <input type="checkbox" checked={onlyHard} onChange={(e) => setOnlyHard(e.target.checked)} /> only revisions, handoffs and S3
        </label>
      </div>
      <ErrorBanner error={res.error} />
      {items.length === 0 ? (
        <Empty>Nothing to review yet.</Empty>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Tier</th>
              <th>Decision</th>
              <th>Why</th>
              <th>Draft</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.slice(0, 60).map((j) => (
              <tr key={j.judgement_id}>
                <td>
                  <TierChip tier={j.tier} />
                </td>
                <td>
                  <DecisionChip decision={j.decision} />
                </td>
                <td className="small ink2">
                  {j.reason}
                  {j.record.failures.length ? `: ${j.record.failures.map((f) => f.dimension).join(", ")}` : ""}
                </td>
                <td className="small" style={{ maxWidth: 360 }}>
                  {j.record.scores.find((s) => s.evidence)?.evidence ?? ""}
                </td>
                <td>
                  {done.has(j.judgement_id) ? (
                    <span className="chip good">reviewed</span>
                  ) : (
                    <button className="btn" onClick={() => open(j.judgement_id, "correct")}>
                      Review
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Corrections({ open }: { open: Open }) {
  const res = useAsync(() => api<Correction[]>("/ops/judge/corrections"), []);
  const rows = res.data ?? [];
  const byVersion = new Map<string, { agree: number; n: number }>();
  for (const c of rows) {
    const v = c.record.rubric_id ?? "unknown";
    const x = byVersion.get(v) ?? { agree: 0, n: 0 };
    x.n += 1;
    x.agree += c.record.agrees_with_decision ? 1 : 0;
    byVersion.set(v, x);
  }
  const agree = rows.filter((c) => c.record.agrees_with_decision).length;
  return (
    <div className="grid two">
      <div className="card">
        <div className="card-head">
          <h2>Judge-reviewer agreement</h2>
          <span className="small muted">{rows.length} corrections</span>
        </div>
        {rows.length ? (
          <>
            <div style={{ fontSize: 24, fontWeight: 650 }} className="tabular">
              {pct(agree / rows.length)}
            </div>
            <div className="small muted" style={{ marginBottom: 10 }}>
              of reviews agreed with the judge's decision and scores (small n: read as a signal, not a measurement)
            </div>
            <h3 style={{ marginBottom: 6 }}>Per rubric version</h3>
            <RateBars rows={[...byVersion.entries()].map(([v, x]) => ({ label: `${v} (n=${x.n})`, value: x.agree / x.n }))} />
          </>
        ) : (
          <Empty>No corrections yet. Review a judgement to start.</Empty>
        )}
      </div>
      <div className="card">
        <div className="card-head">
          <h2 className="row">
            <BookOpen size={16} aria-hidden /> Exemplar set (what the judge sees)
          </h2>
        </div>
        {rows.filter((c) => !c.record.agrees_with_decision).length === 0 ? (
          <Empty>No disagreements yet, so no exemplars.</Empty>
        ) : (
          <div className="stack">
            {rows
              .filter((c) => !c.record.agrees_with_decision)
              .slice(0, 20)
              .map((c) => (
                <div key={c.correction_id} className="card" style={{ margin: 0 }}>
                  <div className="row small">
                    <TierChip tier={c.record.tier} />
                    <span className="chip">{c.split}</span>
                    <span className="muted">{new Date(c.created_at).toLocaleString()}</span>
                    <button className="btn ghost right" onClick={() => open(c.judgement_id)}>
                      open
                    </button>
                  </div>
                  <div className="small" style={{ marginTop: 6 }}>
                    {Object.entries(c.record.dimension_overrides).map(([d, v]) => (
                      <span key={d} className="chip" style={{ marginRight: 4 }}>
                        {d}: {String(c.record.original_scores[d])} → {String(v)}
                      </span>
                    ))}
                    {c.record.decision_override && (
                      <span className="chip">
                        decision: {c.record.original_decision} → {c.record.decision_override}
                      </span>
                    )}
                  </div>
                  {c.record.reason && <div className="small ink2" style={{ marginTop: 4 }}>“{c.record.reason}”</div>}
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Examples() {
  const res = useAsync(() => api<Example[]>("/ops/judge/examples"), []);
  const [prompt, setPrompt] = useState("");
  const [reply, setReply] = useState("");
  const [notes, setNotes] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    setErr(null);
    try {
      await post("/ops/judge/examples", { prompt_text: prompt, expected_reply: reply, notes, split: "live" });
      setPrompt("");
      setReply("");
      setNotes("");
      res.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div className="grid two">
      <div className="card stack">
        <h2>For messages like this, a good reply looks like this</h2>
        <label className="field">
          Passenger message
          <textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="My flight was cancelled and I'm stuck with my baby…" />
        </label>
        <label className="field">
          Expected reply
          <textarea rows={6} value={reply} onChange={(e) => setReply(e.target.value)} />
        </label>
        <label className="field">
          Notes (optional)
          <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="e.g. lead with the hotel, then the €400" />
        </label>
        <div>
          <button className="btn primary" disabled={!prompt.trim() || !reply.trim()} onClick={save}>
            Save example
          </button>
        </div>
        <ErrorBanner error={err} />
      </div>
      <div className="card">
        <h2 style={{ marginBottom: 10 }}>Saved examples</h2>
        {res.data?.length ? (
          <div className="stack">
            {res.data.map((x) => (
              <details key={x.example_id} className="card" style={{ margin: 0 }}>
                <summary className="small">
                  <strong>{x.record.prompt_text.slice(0, 90)}</strong> <span className="muted">{new Date(x.created_at).toLocaleDateString()}</span>
                </summary>
                <div className="draft small" style={{ marginTop: 8 }}>
                  {x.record.expected_reply}
                </div>
                {x.record.notes && <div className="small ink2">{x.record.notes}</div>}
              </details>
            ))}
          </div>
        ) : (
          <Empty>No examples yet.</Empty>
        )}
      </div>
    </div>
  );
}

function Proposals() {
  const res = useAsync(() => api<Proposal[]>("/ops/judge/proposals"), []);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const apply = async (p: Proposal) => {
    setErr(null);
    try {
      const r = await post<{ rubric_version: string }>(`/ops/judge/proposals/${p.proposal_id}/apply`);
      setMsg(`Applied: the rubric is now version ${r.rubric_version}. New judgements record it.`);
      res.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div className="card">
      <h2 style={{ marginBottom: 6 }}>Proposed rubric changes</h2>
      <p className="ink2 small" style={{ marginTop: 0 }}>
        Made automatically when several corrections on the same tier and dimension point the same way. Nothing changes until you apply one.
      </p>
      <ErrorBanner error={res.error ?? err} />
      {msg && <div className="banner" role="status">{msg}</div>}
      {res.data?.length ? (
        <table className="data">
          <thead>
            <tr>
              <th>Tier</th>
              <th>Dimension</th>
              <th>Change</th>
              <th>Evidence</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {res.data.map((p) => (
              <tr key={p.proposal_id}>
                <td>
                  <TierChip tier={p.change.tier} />
                </td>
                <td>{p.dimension}</td>
                <td>{p.change.action.replace(/_/g, " ")} by 1</td>
                <td className="small ink2">{p.change.evidence}</td>
                <td>
                  <span className="chip">{p.status}</span>
                </td>
                <td>
                  {p.status === "open" && (
                    <button className="btn primary" onClick={() => apply(p)}>
                      Apply
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty>No proposals. They appear after a few consistent corrections.</Empty>
      )}
    </div>
  );
}
