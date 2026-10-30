import { useState } from "react";

// Chart colors are the validated categorical slots 1-3 (CSS vars); values always appear as text too.
export const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];

type Segment = { label: string; value: number };

/** Horizontal stacked bars: one row per category, segments in a fixed series order. */
export function StackedBars({ rows, series }: { rows: { label: string; segments: Segment[] }[]; series: string[] }) {
  const [tip, setTip] = useState<string | null>(null);
  return (
    <div className="stack">
      <div className="legend" aria-hidden>
        {series.map((s, i) => (
          <span key={s}>
            <i className="sw" style={{ background: SERIES[i] }} />
            {s}
          </span>
        ))}
      </div>
      <div className="hbar">
        {rows.map((r) => {
          const total = r.segments.reduce((a, b) => a + b.value, 0);
          return (
            <Row key={r.label} label={r.label} total={total}>
              {r.segments.map((seg, i) =>
                seg.value > 0 ? (
                  <span
                    key={seg.label}
                    style={{ width: `${(100 * seg.value) / total}%`, background: SERIES[series.indexOf(seg.label) >= 0 ? series.indexOf(seg.label) : i] }}
                    onMouseEnter={() => setTip(`${r.label} · ${seg.label}: ${seg.value} of ${total} (${Math.round((100 * seg.value) / total)} %)`)}
                    onMouseLeave={() => setTip(null)}
                    title={`${seg.label}: ${seg.value}`}
                  />
                ) : null,
              )}
            </Row>
          );
        })}
      </div>
      <div className="small muted" aria-live="polite" style={{ minHeight: 18 }}>
        {tip ?? "Hover a segment for its count."}
      </div>
      <table className="sr-only">
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <th>{r.label}</th>
              {r.segments.map((s) => (
                <td key={s.label}>
                  {s.label} {s.value}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Row({ label, total, children }: { label: string; total: number; children: React.ReactNode }) {
  return (
    <>
      <span className="small">{label}</span>
      <div className="track">{total ? children : <span style={{ width: "100%", background: "var(--surface-2)" }} />}</div>
      <span className="small tabular right">{total}</span>
    </>
  );
}

/** One series of rates (0-1) as horizontal bars, with a target tick when given. */
export function RateBars({ rows, target }: { rows: { label: string; value: number | null }[]; target?: number }) {
  return (
    <div className="hbar">
      {rows.map((r) => (
        <Rate key={r.label} label={r.label} value={r.value} target={target} />
      ))}
    </div>
  );
}

function Rate({ label, value, target }: { label: string; value: number | null; target?: number }) {
  return (
    <>
      <span className="small">{label}</span>
      <div className="bar" title={value === null ? "no data" : `${Math.round(100 * value)} %`}>
        <span style={{ width: `${100 * (value ?? 0)}%` }} />
        {target !== undefined && <i style={{ left: `${100 * target}%` }} aria-hidden />}
      </div>
      <span className="small tabular right">{value === null ? "n/a" : `${Math.round(100 * value)} %`}</span>
    </>
  );
}

/** Single-series line over time with a crosshair tooltip. */
export function TrendLine({
  points,
  format = (v) => String(v),
  height = 120,
  label,
}: {
  points: { t: number; v: number }[];
  format?: (v: number) => string;
  height?: number;
  label: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 600;
  const pad = { l: 34, r: 8, t: 8, b: 20 };
  if (points.length < 2) return <div className="small muted">Not enough data yet for a trend.</div>;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t || t0 + 1;
  const vMax = Math.max(1, ...points.map((p) => p.v));
  const x = (t: number) => pad.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - pad.l - pad.r);
  const y = (v: number) => pad.t + (1 - v / vMax) * (height - pad.t - pad.b);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const near = (px: number) =>
    points.reduce((best, p, i) => (Math.abs(x(p.t) - px) < Math.abs(x(points[best].t) - px) ? i : best), 0);
  const h = hover === null ? null : points[hover];
  return (
    <div className="chart" aria-label={label} role="img">
      <svg
        viewBox={`0 0 ${W} ${height}`}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          setHover(near(((e.clientX - r.left) / r.width) * W));
        }}
        onMouseLeave={() => setHover(null)}
      >
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={pad.l} x2={W - pad.r} y1={y(f * vMax)} y2={y(f * vMax)} stroke="var(--line)" strokeWidth={1} />
            <text x={pad.l - 6} y={y(f * vMax) + 4} textAnchor="end" fontSize={10} fill="var(--muted)">
              {format(f * vMax)}
            </text>
          </g>
        ))}
        <path d={d} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {h && (
          <>
            <line x1={x(h.t)} x2={x(h.t)} y1={pad.t} y2={height - pad.b} stroke="var(--axis)" strokeWidth={1} />
            <circle cx={x(h.t)} cy={y(h.v)} r={4} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} />
          </>
        )}
        <text x={pad.l} y={height - 4} fontSize={10} fill="var(--muted)">
          {new Date(t0).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </text>
        <text x={W - pad.r} y={height - 4} fontSize={10} fill="var(--muted)" textAnchor="end">
          {new Date(t1).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </text>
      </svg>
      {h && (
        <div className="tip" style={{ left: `${(x(h.t) / W) * 100}%`, top: `${(y(h.v) / height) * 100}%` }}>
          {new Date(h.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · {format(h.v)}
        </div>
      )}
    </div>
  );
}
