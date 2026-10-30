# Ops console

React + TypeScript (Vite). It reads everything from the ops API and holds no business logic. Screens:
- chat with the live pipeline;
- the Judge Monitor;
- the judgement explorer, where each judgement opens its scorecard, diff and correction form;
- the Judge Workbench;
- the handoff queue;
- metrics and fairness;
- settings.

```bash
uv run python -m flightright.api.server   # in the repo root: API on http://127.0.0.1:8000
cd ui
npm ci
npm run dev                               # http://127.0.0.1:5173
```

Open **Settings** and enter the API URL and `OPS_API_KEY`. The key stays in this browser's local
storage. For local development you can preset it in `ui/.env.local`, which is git-ignored:

```
VITE_OPS_API_KEY=...
```

`npm run build` type-checks and writes a production bundle to `ui/dist/`. CI runs the same command.

Live views use server-sent events. They are read with `fetch` rather than `EventSource`, because
`EventSource` cannot send the API key header. A deep link of the form `#/judgement/<id>` opens one
judgement directly.

Icons come from Lucide, under the ISC license (`licenses/lucide-LICENSE.txt`). The logo and charts are
self-made (see `../ASSETS.md`). Motion is switched off when the system asks for reduced motion.
