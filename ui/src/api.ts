// Thin client for the ops API. The console holds no business logic: everything shown comes from here.

export type Settings = { apiUrl: string; apiKey: string; adminKey: string };

const KEY = "flightright.settings";
// Local development may preset these in ui/.env.local (git-ignored); otherwise enter them in Settings.
const DEFAULTS: Settings = {
  apiUrl: import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000",
  apiKey: import.meta.env.VITE_OPS_API_KEY ?? "",
  adminKey: "",
};

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

export function saveSettings(s: Settings): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* private window: settings last for this tab only */
  }
}

let settings = loadSettings();
export const getSettings = () => settings;
export function setSettings(s: Settings): void {
  settings = s;
  saveSettings(s);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function headers(admin = false): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (settings.apiKey) h["X-API-Key"] = settings.apiKey;
  if (admin && settings.adminKey) h["X-Admin-Key"] = settings.adminKey;
  return h;
}

export async function api<T>(path: string, init: RequestInit & { admin?: boolean } = {}): Promise<T> {
  const r = await fetch(settings.apiUrl.replace(/\/$/, "") + path, {
    ...init,
    headers: { ...headers(init.admin), ...(init.headers ?? {}) },
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const body = await r.json();
      msg = body.title ?? body.detail ?? msg;
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg);
  }
  return normalize(await r.json()) as T;
}

/** Judgement records written by the code-checks-only judge omit some fields; fill them once here. */
function normalize(x: unknown): unknown {
  if (Array.isArray(x)) return x.map(normalize);
  if (x && typeof x === "object") {
    const o = x as Record<string, unknown>;
    if (o.record && typeof o.record === "object" && "judgement_id" in o) {
      const rec = o.record as Record<string, unknown>;
      o.record = { failures: [], scores: [], decision_reason: o.reason ?? "", severity_tier: o.tier, ...rec };
    }
    for (const k of ["items"]) if (Array.isArray(o[k])) o[k] = (o[k] as unknown[]).map(normalize);
  }
  return x;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export type StreamEvent = { event: string; conversation_id?: string; payload: Record<string, unknown>; ts: string; attempt_no?: number };

/** Server-sent events over fetch (EventSource cannot send the API key header). */
export function stream(path: string, onEvent: (e: StreamEvent) => void, onError?: (e: unknown) => void): () => void {
  const ctrl = new AbortController();
  (async () => {
    try {
      const r = await fetch(settings.apiUrl.replace(/\/$/, "") + path, { headers: headers(), signal: ctrl.signal });
      if (!r.ok || !r.body) throw new ApiError(r.status, r.statusText);
      const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        let i: number;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i);
          buf = buf.slice(i + 2);
          const data = chunk
            .split("\n")
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trimStart())
            .join("\n");
          if (data) {
            try {
              onEvent(JSON.parse(data));
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      }
    } catch (e) {
      if (!ctrl.signal.aborted) onError?.(e);
    }
  })();
  return () => ctrl.abort();
}

// ---------------------------------------------------------------- response shapes

export type Score = {
  id: string;
  kind: "scale" | "check";
  score: number | null;
  passed: boolean | null;
  evidence: string;
  rationale: string;
  evidence_ok?: boolean;
};

export type JudgementRecord = {
  judgement_id: string;
  target: string;
  severity_tier: string;
  verdict: string;
  decision: string;
  decision_reason: string;
  failures: { dimension: string; reason: string }[];
  scores: Score[];
  analysis?: string;
  revision_guidance?: string | null;
  panel?: { members: string[]; disagreement: boolean; member_passed: boolean[]; member_scores?: Score[][] } | null;
  models?: Record<string, string>;
  error?: string | null;
  rubric_id?: string;
  prompt_version?: string;
  exemplar_count?: number;
  latency_ms?: number;
  model_calls?: number;
  created_at?: string;
};

export type Judgement = {
  judgement_id: string;
  conversation_id: string;
  turn: number;
  attempt_id: string | null;
  target: string;
  tier: string;
  verdict: string;
  decision: string;
  reason: string;
  record: JudgementRecord;
  created_at: string;
  attempt?: { attempt_id: string; draft: string; checks: { check_id: string; passed: boolean; detail: string }[] } | null;
};

export type Diff = {
  judgement_id: string;
  decision: string;
  failed_draft: string;
  revision: string | null;
  guidance: string[];
  failed_checks: { check_id: string; detail: string }[];
};

export type Handoff = {
  ticket_id: string;
  conversation_id: string;
  tier: string;
  urgency: string;
  status: string;
  reason: string;
  sla_due_at: string;
  sla_breached?: boolean;
  record: string | Record<string, unknown>;
  resolution: string | Record<string, unknown> | null;
  created_at: string;
};

export type Metrics = {
  window: string;
  judgements: number;
  decisions_by_tier: Record<string, Record<string, number>>;
  dimension_pass_rate: Record<string, number>;
  panel_disagreement_rate: number | null;
  judge_unavailable_rate: number | null;
  latency_ms_p50: number | null;
  latency_ms_p95: number | null;
  models: Record<string, number>;
  quota_today: Record<string, { requests: number; tokens: number; rpd_limit: number | null }>;
  quality_signals: Record<string, unknown>;
};

export type Notification = { id: number; kind: string; ticket_id: string | null; channel: string; text: string; created_at: string };
