import { AlertTriangle, CheckCircle2, CircleSlash, Inbox, RotateCcw, UserRound } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const load = useCallback(async () => {
    try {
      setData(await fnRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    load();
    if (!refreshMs) return;
    const t = setInterval(load, refreshMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload: load };
}

export const pct = (x: number | null | undefined, digits = 0) =>
  x === null || x === undefined ? "n/a" : `${(100 * x).toFixed(digits)} %`;

export const ms = (x: number | null | undefined) =>
  x === null || x === undefined ? "n/a" : x >= 1000 ? `${(x / 1000).toFixed(1)} s` : `${Math.round(x)} ms`;

export function time(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function TierChip({ tier }: { tier: string | null | undefined }) {
  if (!tier) return null;
  return <span className={`chip tier-${tier}`} title={TIER_HELP[tier] ?? tier}>{tier}</span>;
}

const TIER_HELP: Record<string, string> = {
  S1: "S1: routine (information, past flight)",
  S2: "S2: disrupted today, no special hardship",
  S3: "S3: severe (vulnerable passenger, stranded, repeat contact)",
  S4: "S4: emergency or legal threat: straight to a human",
};

const DECISION_ICON: Record<string, ReactNode> = {
  approve: <CheckCircle2 size={14} aria-hidden />,
  revise: <RotateCcw size={14} aria-hidden />,
  handoff: <UserRound size={14} aria-hidden />,
};

export function DecisionChip({ decision }: { decision: string | null | undefined }) {
  if (!decision) return null;
  const cls = decision === "approve" ? "good" : decision === "revise" ? "warn" : "bad";
  return (
    <span className={`chip ${cls}`}>
      {DECISION_ICON[decision] ?? <CircleSlash size={14} aria-hidden />}
      {decision}
    </span>
  );
}

export function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="banner error" role="alert">
      <AlertTriangle size={16} aria-hidden /> <span>{error}. Check the API URL and key in Settings.</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <Inbox size={28} aria-hidden />
      {children}
    </div>
  );
}

export function PageHead({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {children && <p>{children}</p>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </div>
  );
}
