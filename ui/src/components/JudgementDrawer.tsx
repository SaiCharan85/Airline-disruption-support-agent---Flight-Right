import { X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, post, type Diff, type Judgement } from "../api";
import { DiffView, Scorecard } from "./Scorecard";
import { ErrorBanner, TierChip, useAsync } from "./common";

type Tab = "score" | "diff" | "correct";

export function JudgementDrawer({ id, onClose, initialTab = "score" }: { id: string; onClose: () => void; initialTab?: Tab }) {
  const [tab, setTab] = useState<Tab>(initialTab);
  const j = useAsync(() => api<Judgement>(`/ops/judgements/${id}`), [id]);
  const diff = useAsync(() => api<Diff>(`/ops/judgements/${id}/diff`).catch(() => null), [id]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`Judgement ${id}`}>
        <div className="row">
          <h2>Judgement <span className="mono">{id}</span></h2>
          {j.data && <TierChip tier={j.data.tier} />}
          <button className="btn ghost right" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        {j.data && (
          <div className="small muted">
            conversation <span className="mono">{j.data.conversation_id}</span> · turn {j.data.turn} · {j.data.target.replace("_", " ")} ·{" "}
            {new Date(j.data.created_at).toLocaleString()}
          </div>
        )}
        <div className="tabs" role="tablist">
          {(["score", "diff", "correct"] as Tab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              {t === "score" ? "Scorecard" : t === "diff" ? "Diff" : "Correct the judge"}
            </button>
          ))}
        </div>
        <ErrorBanner error={j.error} />
        {j.data && tab === "score" && <Scorecard record={j.data.record} draft={j.data.attempt?.draft} />}
        {tab === "diff" &&
          (diff.data ? <DiffView diff={diff.data} /> : <div className="muted">No draft attempt behind this judgement.</div>)}
        {j.data && tab === "correct" && <CorrectionForm judgement={j.data} />}
      </aside>
    </>
  );
}

/** FR-16.2: one-click overrides with an optional reason. Test-split items are refused by the API. */
export function CorrectionForm({ judgement, onDone }: { judgement: Judgement; onDone?: () => void }) {
  const scores = judgement.record.scores;
  const [over, setOver] = useState<Record<string, number | boolean>>({});
  const [decision, setDecision] = useState<string>("");
  const [verdict, setVerdict] = useState<string>("");
  const [reason, setReason] = useState("");
  const [split, setSplit] = useState("live");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const submit = async (agree: boolean) => {
    setErr(null);
    try {
      const r = await post<{ correction_id: string; new_proposals: string[] }>(`/ops/judgements/${judgement.judgement_id}/corrections`, {
        split,
        dimension_overrides: agree ? {} : over,
        decision_override: agree ? null : decision || null,
        verdict_override: agree ? null : verdict || null,
        reason: agree ? "agree" : reason,
      });
      setMsg(
        `Saved ${r.correction_id}.` +
          (r.new_proposals.length ? ` The workbench proposed ${r.new_proposals.length} bar change(s): review them under Workbench → Proposals.` : ""),
      );
      setOver({});
      onDone?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  if (!scores.length) return <div className="muted">Nothing to correct: no model scores (a code check decided this one).</div>;
  return (
    <div className="stack" style={{ gap: 12 }}>
      <p className="ink2" style={{ margin: 0 }}>
        Change only what the judge got wrong. Corrections become examples the judge sees next time (most recent first, matched by tier and dimension), and
        consistent ones produce a proposed bar change you can approve. The model itself is never retrained.
      </p>
      <div className="row">
        <button className="btn" onClick={() => submit(true)}>
          The judge was right
        </button>
        <span className="muted small">one click, counts toward agreement</span>
      </div>
      <table className="data">
        <thead>
          <tr>
            <th>Dimension</th>
            <th>Judge</th>
            <th>Your value</th>
          </tr>
        </thead>
        <tbody>
          {scores.map((s) => (
            <tr key={s.id}>
              <td>{s.id}</td>
              <td className="tabular">{s.kind === "scale" ? s.score : s.passed ? "pass" : "fail"}</td>
              <td>
                {s.kind === "scale" ? (
                  <select
                    value={String(over[s.id] ?? "")}
                    onChange={(e) => setOver(({ [s.id]: _, ...rest }) => (e.target.value ? { ...rest, [s.id]: Number(e.target.value) } : rest))}
                    aria-label={`Override ${s.id}`}
                  >
                    <option value="">keep</option>
                    {[1, 2, 3, 4, 5].map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                ) : (
                  <select
                    value={over[s.id] === undefined ? "" : String(over[s.id])}
                    onChange={(e) => setOver(({ [s.id]: _, ...rest }) => (e.target.value ? { ...rest, [s.id]: e.target.value === "true" } : rest))}
                    aria-label={`Override ${s.id}`}
                  >
                    <option value="">keep</option>
                    <option value="true">pass</option>
                    <option value="false">fail</option>
                  </select>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <label className="field">
          Decision should be
          <select value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="">keep ({judgement.decision})</option>
            <option value="approve">approve</option>
            <option value="revise">revise</option>
            <option value="handoff">handoff</option>
          </select>
        </label>
        <label className="field">
          Verdict should be
          <select value={verdict} onChange={(e) => setVerdict(e.target.value)}>
            <option value="">keep ({judgement.verdict})</option>
            <option value="good">good</option>
            <option value="acceptable">acceptable</option>
            <option value="bad">bad</option>
          </select>
        </label>
        <label className="field">
          Split
          <select value={split} onChange={(e) => setSplit(e.target.value)}>
            <option value="live">live / demo</option>
            <option value="dev">dev</option>
            <option value="test">test (refused)</option>
          </select>
        </label>
      </div>
      <label className="field">
        Reason (optional)
        <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. the passenger said this is their second contact" />
      </label>
      <div className="row">
        <button className="btn primary" disabled={!Object.keys(over).length && !decision && !verdict} onClick={() => submit(false)}>
          Save correction
        </button>
        {msg && <span className="small" role="status">{msg}</span>}
      </div>
      <ErrorBanner error={err} />
    </div>
  );
}
