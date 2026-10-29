"""Export the workflows from the running n8n container into n8n/workflows/ (SPEC 8.1).

  uv run python scripts/n8n_export.py

Uses the n8n CLI inside the `n8n` compose service, drops volatile export fields, keeps the
existing file name per workflow ID, and refuses to write anything that fails the secret scan.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from flightright.n8n import WORKFLOW_DIR, env_secret_values, find_secrets, lint, load_all, normalize

ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = "/home/node/flightright-export"


def compose(*args: str) -> None:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    out = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{out.stderr or out.stdout}")


def main() -> int:
    compose("exec", "-T", "n8n", "sh", "-c", f"rm -rf {EXPORT_DIR} && mkdir -p {EXPORT_DIR}")
    compose(
        "exec",
        "-T",
        "n8n",
        "n8n",
        "export:workflow",
        "--all",
        "--separate",
        "--pretty",
        f"--output={EXPORT_DIR}",
    )
    names = {wf["id"]: name for name, wf in load_all().items()}
    secrets = env_secret_values()
    with tempfile.TemporaryDirectory() as tmp:
        compose("cp", f"n8n:{EXPORT_DIR}/.", tmp)
        exported = [json.loads(p.read_text(encoding="utf-8")) for p in Path(tmp).glob("*.json")]
    failed = False
    for wf in exported:
        wf = normalize(wf)
        problems = find_secrets(wf, secrets) + lint(wf)
        if problems:
            failed = True
            print(f"refusing {wf.get('name')}:", *problems, sep="\n  ", file=sys.stderr)
            continue
        name = names.get(wf["id"]) or f"{wf['name'].split()[0].lower()}_{wf['id']}.json"
        (WORKFLOW_DIR / name).write_text(json.dumps(wf, indent=2) + "\n", encoding="utf-8")
        print(f"exported {wf['name']} -> n8n/workflows/{name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
