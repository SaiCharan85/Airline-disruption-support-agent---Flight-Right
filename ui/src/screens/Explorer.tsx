import { ChevronRight } from "lucide-react";
import { useState } from "react";
import type { Open } from "../App";
import { api, type Judgement } from "../api";
import { DecisionChip, Empty, ErrorBanner, PageHead, TierChip, ms, useAsync } from "../components/common";

const DIMS = ["relevance", "context_awareness", "urgency", "tone", "grievance_handling", "correctness", "actionability", "fairness_safety"];

export function Explorer({ open }: { open: Open }) {
  const [f, setF] = useState({ tier: "", decision: "", verdict: "", dimension_failed: "" });
  const [pages, setPages] = useState<string[]>([""]);
  const cursor = pages[pages.length - 1];
  const qs = new URLSearchParams(Object.entries({ ...f, cursor }).filter(([, v]) => v) as [string, string][]);
  qs.set("limit", "50");
  const res = useAsync(() => api<{ items: Judgement[]; next_cursor: string | null }>(`/ops/judgements?${qs}`), [qs.toString()]);
  const set = (k: keyof typeof f, v: string) => {
    setF({ ...f, [k]: v });
    setPages([""]);
  };
  return (
    <>
      <PageHead title="Judgements">Every stored judgement, newest first. Filter, then open one for its scorecard, diff and correction form.</PageHead>
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <Sel label="Tier" value={f.tier} onChange={(v) => set("tier", v)} options={["S1", "S2", "S3", "S4"]} />
          <Sel label="Decision" value={f.decision} onChange={(v) => set("decision", v)} options={["approve", "revise", "handoff"]} />
          <Sel label="Verdict" value={f.verdict} onChange={(v) => set("verdict", v)} options={["good", "acceptable", "bad"]} />
          <Sel label="Failed dimension" value={f.dimension_failed} onChange={(v) => set("dimension_failed", v)} options={DIMS} />
        </div>
        <ErrorBanner error={res.error} />
        {res.data?.items.length === 0 ? (
          <Empty>No judgements match.</Empty>
        ) : (
          <div className="scroll-x">
            <table className="data">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Tier</th>
                  <th>Target</th>
                  <th>Decision</th>
                  <th>Verdict</th>
                  <th>Why</th>
                  <th>Latency</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {res.data?.items.map((j) => (
                  <tr key={j.judgement_id} className="clickable" tabIndex={0} onClick={() => open(j.judgement_id)} onKeyDown={(e) => e.key === "Enter" && open(j.judgement_id)}>
                    <td className="small tabular">{new Date(j.created_at).toLocaleString()}</td>
                    <td>
                      <TierChip tier={j.tier} />
                    </td>
                    <td className="small">{j.target.replace("_", " ")}</td>
                    <td>
                      <DecisionChip decision={j.decision} />
                    </td>
                    <td className="small">{j.verdict}</td>
                    <td className="small ink2">
                      {j.reason}
                      {j.record.failures.length ? `: ${j.record.failures.map((x) => x.dimension).join(", ")}` : ""}
                    </td>
                    <td className="small tabular">{ms(j.record.latency_ms)}</td>
                    <td>
                      <ChevronRight size={15} aria-hidden />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn" disabled={pages.length < 2} onClick={() => setPages(pages.slice(0, -1))}>
            Newer
          </button>
          <button className="btn" disabled={!res.data?.next_cursor} onClick={() => setPages([...pages, res.data!.next_cursor!])}>
            Older
          </button>
        </div>
      </div>
    </>
  );
}

function Sel({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <label className="field">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">any</option>
        {options.map((o) => (
          <option key={o}>{o}</option>
        ))}
      </select>
    </label>
  );
}
