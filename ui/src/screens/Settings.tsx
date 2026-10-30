import { CheckCircle2, KeyRound } from "lucide-react";
import { useState } from "react";
import { api, getSettings, setSettings } from "../api";
import { ErrorBanner, PageHead } from "../components/common";

type Health = { status: string; git_commit: string; rulesets: Record<string, string>; rubric_id: string; time: string };

export function Settings({ onSaved }: { onSaved: () => void }) {
  const [s, setS] = useState(getSettings());
  const [health, setHealth] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const test = async () => {
    setSettings(s);
    setErr(null);
    try {
      setHealth(await api<Health>("/healthz"));
      await api("/ops/metrics/judge?window=15m");
    } catch (e) {
      setHealth(null);
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <>
      <PageHead title="Settings">
        Where the ops API runs and the keys to call it. Keys stay in this browser (local storage) and are sent only to that API.
      </PageHead>
      <div className="card stack" style={{ maxWidth: 560 }}>
        <label className="field">
          API URL
          <input value={s.apiUrl} onChange={(e) => setS({ ...s, apiUrl: e.target.value })} />
        </label>
        <label className="field">
          Ops API key (OPS_API_KEY)
          <input type="password" value={s.apiKey} onChange={(e) => setS({ ...s, apiKey: e.target.value })} autoComplete="off" />
        </label>
        <label className="field">
          Admin key (optional; only for unmasking personal data)
          <input type="password" value={s.adminKey} onChange={(e) => setS({ ...s, adminKey: e.target.value })} autoComplete="off" />
        </label>
        <div className="row">
          <button className="btn" onClick={test}>
            <KeyRound size={15} /> Test connection
          </button>
          <button
            className="btn primary"
            onClick={() => {
              setSettings(s);
              onSaved();
            }}
          >
            Save
          </button>
        </div>
        {health && (
          <div className="banner" role="status">
            <CheckCircle2 size={16} color="var(--good)" aria-hidden />
            <span>
              Connected. Commit <span className="mono">{health.git_commit}</span>, rubric <span className="mono">{health.rubric_id}</span>, rulesets{" "}
              {Object.entries(health.rulesets).map(([k, v]) => `${k} ${v}`).join(", ")}.
            </span>
          </div>
        )}
        <ErrorBanner error={err} />
      </div>
    </>
  );
}
