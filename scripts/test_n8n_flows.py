"""n8n acceptance test (SPEC 8.3) against the running stack.

  uv run python -m flightright.api.server            # API on the host (or --profile api)
  SLA_SECONDS_OVERRIDE=10 docker compose up -d n8n   # short SLA for the test stack
  uv run python scripts/n8n_import.py
  uv run python scripts/test_n8n_flows.py

Checks: W1 replies exactly once per message ID; a handoff produces a signed event and a ticket
notice (W2); an unacknowledged ticket is escalated after the SLA; an acknowledged one is not;
W5 resolves a ticket in the API; forged events are dropped and logged; W3 writes a digest; the
exported workflow JSON holds no secrets. Uses a few free LLM calls (triage, handoff summary).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from flightright.config import ROOT, load_env
from flightright.n8n import env_secret_values, find_secrets, load_all

N8N = os.environ.get("N8N_URL", "http://127.0.0.1:5678")
API = os.environ.get("FLIGHTRIGHT_API_URL_HOST", "http://127.0.0.1:8000")
HUMAN = "My flight was cancelled. I want to speak to a human agent, please."


class Flow:
    def __init__(self) -> None:
        load_env()
        self.http = httpx.Client(timeout=180)
        self.ops = {"X-API-Key": os.environ.get("OPS_API_KEY", "")}
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: Any = "") -> None:
        print(
            f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else "")
        )
        if not ok:
            self.failures.append(label)

    def inbound(self, user: str, text: str, message_id: str) -> dict[str, Any]:
        r = self.http.post(
            f"{N8N}/webhook/inbound",
            json={"user_id": user, "text": text, "message_id": message_id},
        )
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    def notifications(self, ticket_id: str | None = None, kind: str | None = None) -> list[Any]:
        params = {k: v for k, v in {"ticket_id": ticket_id, "kind": kind}.items() if v}
        r = self.http.get(f"{API}/ops/notifications", headers=self.ops, params=params)
        return r.json()  # type: ignore[no-any-return]

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        return self.http.get(f"{API}/ops/handoffs/{ticket_id}", headers=self.ops).json()  # type: ignore[no-any-return]

    @staticmethod
    def until(fn: Callable[[], Any], timeout_s: float, every: float = 1.0) -> Any:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(every)
        return fn()


def sla_override() -> int:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "n8n", "printenv", "SLA_SECONDS_OVERRIDE"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        return int(out.stdout.strip() or 0)
    except ValueError:
        return 0


def main() -> int:
    f = Flow()
    try:
        f.http.get(f"{API}/healthz").raise_for_status()
        f.http.get(f"{N8N}/healthz").raise_for_status()
    except httpx.HTTPError as exc:
        print(f"stack not running: {exc}", file=sys.stderr)
        return 2
    sla = sla_override()
    if not 0 < sla <= 60:
        print("start n8n with SLA_SECONDS_OVERRIDE=10 (test stack) first", file=sys.stderr)
        return 2
    run = uuid.uuid4().hex[:8]

    # W1: reply once per message ID
    user = f"acceptance-{run}"
    first = f.inbound(user, "Hello, my flight was delayed yesterday. What can I get?", f"{run}-1")
    again = f.inbound(user, "Hello, my flight was delayed yesterday. What can I get?", f"{run}-1")
    cid = first.get("conversation_id")
    f.check("W1 reply returned with a conversation", bool(first.get("reply_text") and cid), first)
    f.check(
        "W1 duplicate delivery returns the same reply, flagged duplicate",
        again.get("duplicate") is True and again.get("reply_text") == first.get("reply_text"),
        again,
    )
    judged = f.http.get(f"{API}/ops/conversations/{cid}/judgements", headers=f.ops).json()
    f.check("W1 duplicate ran no second pipeline", len(judged) <= 1, f"{len(judged)} judgements")
    bad = f.http.post(f"{N8N}/webhook/inbound", json={"user_id": user})
    f.check("W1 rejects a message without text", bad.status_code == 400, bad.status_code)

    # W2: handoff -> signed event -> notice -> SLA escalation
    out = f.inbound(f"{user}-h1", HUMAN, f"{run}-h1")
    t1 = str(out.get("ticket_id") or "")
    f.check("handoff reply carries a ticket", out.get("status") == "handed_off" and bool(t1), out)
    notice = f.until(lambda: f.notifications(t1, "handoff.created"), 20)
    f.check("W2 verified the signed event and posted the ticket notice", bool(notice))

    # a second ticket that is acknowledged in time (via W5) must not escalate
    t2 = f.inbound(f"{user}-h2", HUMAN, f"{run}-h2").get("ticket_id")
    ack = f.http.post(
        f"{N8N}/webhook/handoff-action-api", json={"ticket_id": t2, "action": "acknowledge"}
    ).json()
    f.check("W5 acknowledge", ack.get("ok") is True and ack.get("status") == "acknowledged", ack)

    escalated = f.until(lambda: f.notifications(t1, "sla.escalated"), sla + 40)
    f.check("W2 escalated the unacknowledged ticket after the SLA", bool(escalated))
    f.check("API marks the ticket escalated", f.ticket(t1).get("status") == "escalated")
    time.sleep(3)
    f.check("acknowledged ticket was not escalated", not f.notifications(t2, "sla.escalated"))

    # W5: resolution changes API state; validation errors come back
    missing = f.http.post(
        f"{N8N}/webhook/handoff-action-api", json={"ticket_id": t1, "action": "resolve"}
    ).json()
    f.check("W5 shows validation errors", missing.get("ok") is False, missing)
    done = f.http.post(
        f"{N8N}/webhook/handoff-action-api",
        json={
            "ticket_id": t1,
            "action": "resolve",
            "decision": "rebooked, compensation approved",
            "compensation_paid": 400,
            "notes": "acceptance test",
        },
    ).json()
    f.check("W5 resolve", done.get("ok") is True, done)
    resolved = f.ticket(t1)
    f.check(
        "API ticket resolved with the paid amount",
        resolved.get("status") == "resolved"
        and (resolved.get("resolution") or {}).get("compensation_paid_eur") == 400,
        resolved.get("status"),
    )

    # forged events are dropped and logged
    before = len(f.notifications(kind="signature.rejected"))
    f.http.post(
        f"{N8N}/webhook/flightright-events",
        content=b'{"event_id":"EV-forged","event":"handoff.created","payload":{}}',
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": str(int(time.time())),
            "X-Signature": "sha256=" + "0" * 64,
        },
    )
    rejected = f.until(lambda: len(f.notifications(kind="signature.rejected")) > before, 15)
    f.check("forged event dropped and logged", bool(rejected))

    # W3 digest
    digest = f.http.post(f"{N8N}/webhook/digest-now", json={})
    latest = f.notifications(kind="digest")
    f.check(
        "W3 digest written",
        digest.status_code == 200 and bool(latest) and "digest" in latest[0]["text"],
    )

    # exported JSON holds no secrets
    leaks = [
        f"{name}: {p}"
        for name, wf in load_all().items()
        for p in find_secrets(wf, env_secret_values())
    ]
    f.check("exported workflows hold no secrets", not leaks, leaks)

    print(f"\n{'all checks passed' if not f.failures else f'{len(f.failures)} failed'}")
    return 1 if f.failures else 0


if __name__ == "__main__":
    sys.exit(main())
