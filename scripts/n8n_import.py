"""Import n8n/workflows/*.json into the running n8n container, publish them and restart n8n.

  docker compose up -d n8n
  uv run python scripts/n8n_import.py

Uses the n8n CLI inside the `n8n` compose service (SPEC 8.1). Re-running replaces the workflows
with the same IDs, so the committed JSON stays the source of truth.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "n8n" / "workflows"


def compose(*args: str) -> str:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    out = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{out.stderr or out.stdout}")
    return out.stdout + out.stderr


def wait_healthy(url: str = "http://127.0.0.1:5678/healthz", timeout_s: int = 90) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit(f"n8n did not become healthy at {url}")


def wait_webhooks(url: str = "http://127.0.0.1:5678/webhook/inbound", timeout_s: int = 60) -> None:
    """Published triggers register a few seconds after /healthz; W1 answers 400 to an empty body."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        req = urllib.request.Request(
            url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit("n8n webhooks did not register; check `docker compose logs n8n`")


def main() -> int:
    files = sorted(WORKFLOWS.glob("*.json"))
    if not files:
        print("no workflows in n8n/workflows", file=sys.stderr)
        return 1
    print(
        compose(
            "exec", "-T", "n8n", "n8n", "import:workflow", "--separate", "--input=/workflows"
        ).strip()
    )
    for path in files:
        wid = json.loads(path.read_text(encoding="utf-8"))["id"]
        compose("exec", "-T", "n8n", "n8n", "publish:workflow", f"--id={wid}")
        print(f"published {wid} ({path.name})")
    compose("restart", "n8n")  # published workflows register their triggers on start
    wait_healthy()
    wait_webhooks()
    print("n8n restarted, webhooks live: http://localhost:5678")
    return 0


if __name__ == "__main__":
    sys.exit(main())
