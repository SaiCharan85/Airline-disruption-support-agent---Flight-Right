import { BellRing, CheckCircle2, Clock } from "lucide-react";
import { useEffect, useState } from "react";
import { api, post, type Handoff, type Notification } from "../api";
import { Empty, ErrorBanner, PageHead, TierChip, useAsync } from "../components/common";

function useNow(every = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(t);
  }, [every]);
  return now;
}

function Countdown({ due, now }: { due: string; now: number }) {
  const left = new Date(due).getTime() - now;
  const late = left < 0;
  const abs = Math.abs(left);
  const h = Math.floor(abs / 3600000);
  const m = Math.floor((abs % 3600000) / 60000);
  const s = Math.floor((abs % 60000) / 1000);
  const txt = h > 48 ? `${Math.round(h / 24)} days` : `${h ? `${h}h ` : ""}${m}m ${String(s).padStart(2, "0")}s`;
  return (
    <span className={`countdown tabular ${late ? "late" : ""}`}>
      <Clock size={13} aria-hidden /> {late ? `${txt} overdue` : `${txt} left`}
    </span>
  );
}

type Ticket = { summary?: string; recommended_next_step?: string; entitlement?: { compensation_eur?: number | null } };

export function Handoffs() {
  const [status, setStatus] = useState("open");
  const list = useAsync(() => api<Handoff[]>(`/ops/handoffs${status ? `?status=${status}` : ""}`), [status], 10000);
  const notes = useAsync(() => api<Notification[]>("/ops/notifications"), [], 10000);
  const [resolving, setResolving] = useState<Handoff | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const now = useNow();
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      list.reload();
      notes.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <>
      <PageHead
        title="Handoff queue"
        actions={
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            {["open", "acknowledged", "escalated", "resolved", ""].map((s) => (
              <option key={s} value={s}>
                {s || "all"}
              </option>
            ))}
          </select>
        }
      >
        Tickets the agent handed to a human, with the SLA clock per tier (S4 10 min, S3 15, S2 60, S1 240). n8n escalates a ticket nobody acknowledged
        in time; the same actions are in its form (W5).
      </PageHead>
      <ErrorBanner error={list.error ?? err} />
      <div className="grid chat">
        <div className="card">
          {list.data?.length === 0 ? (
            <Empty>No {status} tickets.</Empty>
          ) : (
            <div className="stack">
              {list.data?.map((h) => {
                const rec = (typeof h.record === "string" ? JSON.parse(h.record) : h.record) as Ticket;
                return (
                  <div key={h.ticket_id} className="card" style={{ margin: 0 }}>
                    <div className="row">
                      <strong className="mono">{h.ticket_id}</strong>
                      <TierChip tier={h.tier} />
                      <span className="chip">{h.status}</span>
                      <span className="chip">{h.urgency}</span>
                      <span className="right">{["open", "escalated"].includes(h.status) && <Countdown due={h.sla_due_at} now={now} />}</span>
                    </div>
                    <p style={{ margin: "8px 0" }}>{rec.summary}</p>
                    <div className="small ink2">
                      Reason: {h.reason}
                      {rec.recommended_next_step && <> · next step: {rec.recommended_next_step}</>}
                      {rec.entitlement?.compensation_eur ? <> · owed: €{rec.entitlement.compensation_eur}</> : null}
                    </div>
                    {h.status !== "resolved" && (
                      <div className="row" style={{ marginTop: 8 }}>
                        {h.status !== "acknowledged" && (
                          <button className="btn" onClick={() => act(() => post(`/ops/handoffs/${h.ticket_id}/acknowledge`))}>
                            Acknowledge
                          </button>
                        )}
                        <button className="btn primary" onClick={() => setResolving(h)}>
                          Resolve…
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <h2 className="row">
              <BellRing size={16} aria-hidden /> Notifications from n8n
            </h2>
          </div>
          {notes.data?.length ? (
            <div className="stack">
              {notes.data.slice(0, 40).map((n) => (
                <div key={n.id} className="small">
                  <span className="chip">{n.kind}</span> <span className="muted">{new Date(n.created_at).toLocaleTimeString()}</span>
                  <div style={{ whiteSpace: "pre-wrap", marginTop: 2 }}>{n.text}</div>
                </div>
              ))}
            </div>
          ) : (
            <Empty>Nothing logged yet. Start n8n to see ticket notices, escalations and digests here.</Empty>
          )}
        </div>
      </div>
      {resolving && (
        <ResolveDialog
          h={resolving}
          onClose={() => setResolving(null)}
          onSubmit={(body) =>
            act(async () => {
              await post(`/ops/handoffs/${resolving.ticket_id}/resolve`, body);
              setResolving(null);
            })
          }
        />
      )}
    </>
  );
}

function ResolveDialog({ h, onClose, onSubmit }: { h: Handoff; onClose: () => void; onSubmit: (b: object) => void }) {
  const [decision, setDecision] = useState("");
  const [notes, setNotes] = useState("");
  const [paid, setPaid] = useState("");
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Resolve ticket" style={{ width: "min(520px, 96vw)" }}>
        <h2>Resolve {h.ticket_id}</h2>
        <p className="ink2">The outcome is stored with the passenger's history, so the agent knows it next time they write.</p>
        <div className="stack">
          <label className="field">
            Decision
            <input value={decision} onChange={(e) => setDecision(e.target.value)} placeholder="e.g. rebooked, compensation approved" />
          </label>
          <label className="field">
            Compensation paid (EUR, optional)
            <input type="number" min={0} value={paid} onChange={(e) => setPaid(e.target.value)} />
          </label>
          <label className="field">
            Notes
            <textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>
          <div className="row">
            <button className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              className="btn primary"
              disabled={!decision.trim()}
              onClick={() => onSubmit({ decision, notes, compensation_paid: paid === "" ? null : Number(paid) })}
            >
              <CheckCircle2 size={15} /> Resolve
            </button>
          </div>
        </div>
      </aside>
    </>
  );
}
