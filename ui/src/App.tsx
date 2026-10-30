import { Activity, BarChart3, GraduationCap, Inbox, MessagesSquare, Moon, Search, Settings as Cog, Sun } from "lucide-react";
import { Component, useEffect, useState, type ReactNode } from "react";
import { getSettings } from "./api";
import { JudgementDrawer } from "./components/JudgementDrawer";
import { Chat } from "./screens/Chat";
import { Explorer } from "./screens/Explorer";
import { Handoffs } from "./screens/Handoffs";
import { Metrics } from "./screens/Metrics";
import { Monitor } from "./screens/Monitor";
import { Settings } from "./screens/Settings";
import { Workbench } from "./screens/Workbench";

const SCREENS: { id: string; label: string; icon: ReactNode }[] = [
  { id: "chat", label: "Chat + pipeline", icon: <MessagesSquare size={17} /> },
  { id: "monitor", label: "Judge Monitor", icon: <Activity size={17} /> },
  { id: "explorer", label: "Judgements", icon: <Search size={17} /> },
  { id: "workbench", label: "Judge Workbench", icon: <GraduationCap size={17} /> },
  { id: "handoffs", label: "Handoff queue", icon: <Inbox size={17} /> },
  { id: "metrics", label: "Metrics + fairness", icon: <BarChart3 size={17} /> },
  { id: "settings", label: "Settings", icon: <Cog size={17} /> },
];

function useHash(): [string, (h: string) => void] {
  const read = () => window.location.hash.replace(/^#\/?/, "") || (getSettings().apiKey ? "chat" : "settings");
  const [hash, setHash] = useState(read);
  useEffect(() => {
    const on = () => setHash(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [hash, (h) => (window.location.hash = `/${h}`)];
}

export type Open = (judgementId: string, tab?: "score" | "diff" | "correct") => void;

export function App() {
  const [hash, go] = useHash();
  const [screen, ...rest] = hash.split("/");
  const [drawer, setDrawer] = useState<{ id: string; tab?: "score" | "diff" | "correct" } | null>(null);
  const [theme, setTheme] = useState<string | null>(() => {
    try {
      return localStorage.getItem("flightright.theme");
    } catch {
      return null;
    }
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    try {
      if (theme) localStorage.setItem("flightright.theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  const open: Open = (id, tab) => setDrawer({ id, tab });
  useEffect(() => {
    // Deep link: #/judgement/<id>[/<tab>] opens that judgement over the explorer.
    if (screen === "judgement" && rest[0]) setDrawer({ id: rest[0], tab: rest[1] as "score" | "diff" | "correct" | undefined });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash]);
  const dark = theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  return (
    <div className="shell">
      <nav className="nav" aria-label="Screens">
        <div className="brand">
          <img src="/logo.svg" alt="" />
          <div>
            Flightright
            <small>support agent · ops console</small>
          </div>
        </div>
        {SCREENS.map((s) => (
          <button key={s.id} aria-current={screen === s.id ? "page" : undefined} onClick={() => go(s.id)}>
            {s.icon}
            {s.label}
          </button>
        ))}
        <div className="spacer" />
        <button onClick={() => setTheme(dark ? "light" : "dark")} aria-label="Toggle dark mode">
          {dark ? <Sun size={17} /> : <Moon size={17} />} {dark ? "Light" : "Dark"} mode
        </button>
      </nav>
      <main className="main">
        <Boundary key={screen}>
        {screen === "chat" && <Chat open={open} />}
        {screen === "monitor" && <Monitor open={open} />}
        {screen === "explorer" && <Explorer open={open} />}
        {screen === "workbench" && <Workbench open={open} initial={rest[0]} />}
        {screen === "handoffs" && <Handoffs />}
        {screen === "metrics" && <Metrics />}
        {screen === "settings" && <Settings onSaved={() => go("chat")} />}
        {screen === "judgement" && <Explorer open={open} />}
        {!SCREENS.some((s) => s.id === screen) && screen !== "judgement" && <Chat open={open} />}
        </Boundary>
      </main>
      {drawer && <JudgementDrawer id={drawer.id} initialTab={drawer.tab} onClose={() => setDrawer(null)} />}
    </div>
  );
}

class Boundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
  render() {
    if (this.state.error)
      return (
        <div className="banner error" role="alert">
          This screen failed to render: {this.state.error}
        </div>
      );
    return this.props.children;
  }
}
