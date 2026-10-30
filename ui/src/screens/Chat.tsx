import {
  BadgeEuro,
  CheckCheck,
  FileText,
  Gavel,
  Loader2,
  PenLine,
  Play,
  RotateCcw,
  Send,
  ShieldAlert,
  ShieldCheck,
  Siren,
  UserRound,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Open } from "../App";
import { api, post, stream, type Judgement, type StreamEvent } from "../api";
import { Scorecard } from "../components/Scorecard";
import { DecisionChip, Empty, ErrorBanner, PageHead, TierChip, ms, time, useAsync } from "../components/common";

type Demo = {
  scenario_id: string;
  booking_ref: string;
  gold_tier: string;
  disruption_type: string;
  timing: string;
  passenger_name: string;
  opening_message: string;
};
type Msg = { role: "passenger" | "agent"; text: string; meta?: string };

const STEP: Record<string, { title: string; icon: ReactNode; detail: (p: Record<string, unknown>) => string }> = {
  "guardrail.injection_detected": { title: "Guardrail blocked an injection attempt", icon: <ShieldAlert size={13} />, detail: () => "" },
  "triage.completed": {
    title: "Triage",
    icon: <Siren size={13} />,
    detail: (p) => `severity ${p.tier}, urgency ${p.urgency}`,
  },
  "entitlement.computed": {
    title: "Rules engine",
    icon: <BadgeEuro size={13} />,
    detail: (p) =>
      `${p.regime}: ${p.compensation_eur ? `€${p.compensation_eur} compensation` : "no compensation"}${p.confidence === "unverified" ? " (needs review)" : ""}`,
  },
  "revision.started": {
    title: "Revision requested",
    icon: <RotateCcw size={13} />,
    detail: (p) => ((p.guidance as string[]) ?? []).slice(0, 2).join(" · "),
  },
  "draft.created": { title: "Agent drafted a reply", icon: <PenLine size={13} />, detail: (p) => `attempt ${p.attempt_no} · ${p.model ?? ""}` },
  "checks.completed": {
    title: "Code checks",
    icon: <ShieldCheck size={13} />,
    detail: (p) => ((p.failed as string[]) ?? []).length ? `failed: ${(p.failed as string[]).join(", ")}` : "all passed",
  },
  "judgement.completed": {
    title: "LLM judge",
    icon: <Gavel size={13} />,
    detail: (p) =>
      [
        p.tier && `${p.tier} bar`,
        p.verdict && `verdict ${p.verdict}`,
        ((p.failed_dimensions as string[]) ?? []).length && `fails ${(p.failed_dimensions as string[]).join(", ")}`,
        p.panel_disagreement && "panel disagreed",
        p.latency_ms !== undefined && ms(p.latency_ms as number),
        `→ ${p.decision}`,
      ]
        .filter(Boolean)
        .join(" · "),
  },
  "handoff.created": {
    title: "Handed to a human",
    icon: <UserRound size={13} />,
    detail: (p) => `ticket ${p.ticket_id} · ${p.reason} · due in ${p.sla_minutes ?? "?"} min`,
  },
  "reply.sent": { title: "Reply sent", icon: <CheckCheck size={13} />, detail: (p) => String(p.status ?? "") },
};

export function Chat({ open }: { open: Open }) {
  const [tier, setTier] = useState("S3");
  const demos = useAsync(() => api<Demo[]>(`/ops/demo/scenarios?tier=${tier}&limit=40`), [tier]);
  const [demo, setDemo] = useState<Demo | null>(null);
  const [cid, setCid] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [latest, setLatest] = useState<Judgement | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!cid) return;
    setEvents([]);
    return stream(`/ops/conversations/${cid}/stream`, (e) => {
      setEvents((xs) => [...xs, e]);
      const jid = (e.payload as { judgement_id?: string }).judgement_id;
      if (e.event === "judgement.completed" && jid) {
        api<Judgement>(`/ops/judgements/${jid}`).then(setLatest).catch(() => undefined);
      }
    });
  }, [cid]);
  useEffect(() => logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" }), [msgs, busy]);

  const start = async (d: Demo) => {
    setErr(null);
    setDemo(d);
    setMsgs([]);
    setLatest(null);
    try {
      const r = await post<{ conversation_id: string }>("/conversations", { channel: "web", booking_ref: d.booking_ref });
      setCid(r.conversation_id);
      setText(d.opening_message);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  const send = async () => {
    if (!cid || !text.trim()) return;
    const t = text.trim();
    setMsgs((m) => [...m, { role: "passenger", text: t }]);
    setText("");
    setBusy(true);
    setErr(null);
    try {
      const r = await post<{ reply_text: string; status: string; ticket_id: string | null }>(`/conversations/${cid}/messages`, {
        text: t,
        idempotency_key: crypto.randomUUID(),
      });
      setMsgs((m) => [...m, { role: "agent", text: r.reply_text, meta: r.status === "handed_off" ? `handed off · ticket ${r.ticket_id}` : "answered" }]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead title="Chat + live pipeline">
        Talk to the agent as a passenger from a demo case. Each step of the pipeline lights up as it happens (server-sent events), and the judge's
        scorecard for the latest draft appears underneath.
      </PageHead>
      <div className="grid chat">
        <div className="card">
          {!cid ? (
            <div className="stack">
              <div className="row">
                <h2>Pick a demo case</h2>
                <select className="right" value={tier} onChange={(e) => setTier(e.target.value)} aria-label="Severity tier">
                  {["S1", "S2", "S3", "S4"].map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </div>
              <ErrorBanner error={demos.error} />
              {demos.data?.length === 0 && <Empty>No demo cases for this tier.</Empty>}
              <div className="stack" style={{ maxHeight: "60vh", overflowY: "auto" }}>
                {demos.data?.map((d) => (
                  <button key={d.scenario_id} className="btn" style={{ justifyContent: "flex-start", textAlign: "left" }} onClick={() => start(d)}>
                    <Play size={14} aria-hidden />
                    <span>
                      <strong>{d.passenger_name}</strong> · {d.disruption_type.replace("_", " ")} · {d.timing.replace("_", " ")}{" "}
                      <span className="mono muted">{d.booking_ref}</span>
                      <br />
                      <span className="small ink2">{d.opening_message.slice(0, 140)}…</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              <div className="row" style={{ marginBottom: 8 }}>
                <h2>{demo?.passenger_name}</h2>
                <TierChip tier={demo?.gold_tier} />
                <span className="mono small muted">{cid}</span>
                <button className="btn ghost right" onClick={() => setCid(null)}>
                  New case
                </button>
              </div>
              <div className="chat-log" ref={logRef} aria-live="polite">
                {msgs.length === 0 && <div className="muted small">The case's opening message is ready below; edit it or send it as is.</div>}
                {msgs.map((m, i) => (
                  <div key={i} className={`bubble ${m.role}`}>
                    {m.text}
                    {m.meta && <span className="meta">{m.meta}</span>}
                  </div>
                ))}
                {busy && (
                  <div className="bubble agent muted">
                    <Loader2 size={14} className="spin" aria-hidden /> drafting, checking and judging…
                  </div>
                )}
              </div>
              <div className="composer">
                <textarea
                  rows={3}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && (e.ctrlKey || e.metaKey) && send()}
                  placeholder="Write as the passenger (Ctrl+Enter to send)"
                  aria-label="Message"
                />
                <button className="btn primary" onClick={send} disabled={busy || !text.trim()}>
                  <Send size={15} /> Send
                </button>
              </div>
            </>
          )}
          <ErrorBanner error={err} />
        </div>
        <div className="stack" style={{ gap: 14 }}>
          <div className="card">
            <div className="card-head">
              <h2>Pipeline</h2>
              <span className="small muted">{events.length} events</span>
            </div>
            {events.length === 0 ? (
              <div className="small muted">Events appear here as the conversation runs.</div>
            ) : (
              <ol className="steps">
                {events.map((e, i) => {
                  const s = STEP[e.event] ?? { title: e.event, icon: <FileText size={13} />, detail: () => "" };
                  const p = e.payload ?? {};
                  const isLast = i === events.length - 1;
                  const cls = [
                    "step",
                    "enter",
                    isLast && busy ? "active" : "done",
                    e.event === "revision.started" ? "loop" : "",
                    e.event === "checks.completed" && ((p.failed as string[]) ?? []).length ? "fail" : "",
                  ].join(" ");
                  const jid = (p as { judgement_id?: string }).judgement_id;
                  return (
                    <li key={i} className={cls}>
                      <span className="dot" aria-hidden>
                        {s.icon}
                      </span>
                      <span>
                        <span className="title">{s.title}</span>
                        <span className="detail"> {s.detail(p)}</span>
                        {jid && (
                          <>
                            {" "}
                            <button className="btn ghost small" onClick={() => open(jid)}>
                              open scorecard
                            </button>
                          </>
                        )}
                      </span>
                      <span className="small muted tabular">{time(e.ts)}</span>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
          <div className="card">
            <div className="card-head">
              <h2>Latest judgement</h2>
              {latest && (
                <div className="row">
                  <DecisionChip decision={latest.decision} />
                  <button className="btn" onClick={() => open(latest.judgement_id, "diff")}>
                    Diff
                  </button>
                  <button className="btn" onClick={() => open(latest.judgement_id, "correct")}>
                    Correct
                  </button>
                </div>
              )}
            </div>
            {latest ? <Scorecard record={latest.record} draft={latest.attempt?.draft} /> : <div className="small muted">No judgement yet.</div>}
          </div>
        </div>
      </div>
    </>
  );
}
