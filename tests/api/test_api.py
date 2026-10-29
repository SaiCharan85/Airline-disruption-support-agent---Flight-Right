"""The HTTP API (SPEC §7) with scripted LLMs: customer and ops endpoints, SSE, Workbench."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from flightright.agent.judging import CodeChecksOnlyJudge, Judge
from flightright.agent.testing import GOOD_DRAFT, SUMMARY, case
from flightright.api.app import create_app
from flightright.api.runtime import EventPublisher, Runtime, RuntimeOptions, sign, verify
from flightright.config import RoleConfig, load_config
from flightright.judge import SeverityAwareJudge
from flightright.llm.cache import LLMCache
from flightright.llm.client import LLMClient
from flightright.llm.testing import ScriptedProvider, models_cfg
from flightright.llm.usage import UsageLedger
from flightright.models import Booking
from flightright.rules.engine import EntitlementEngine
from flightright.triage.triage import Triage

JudgeFactory = Callable[[Runtime], Judge]
KEY, ADMIN = "ops-key", "admin-key"
OPS = {"X-API-Key": KEY}
NOW = datetime(2027, 11, 15, 12, tzinfo=UTC)


class Setup:
    def __init__(
        self,
        engine: EntitlementEngine,
        tmp: Path,
        script: list[str],
        judge: JudgeFactory | None = None,
    ) -> None:
        cfg = load_config()
        mcfg = models_cfg(
            roles={
                r: RoleConfig(chain=["a:m1"], max_tokens=50)
                for r in ("agent", "summary", "judge_a", "judge_b")
            }
        )
        self.provider = ScriptedProvider("a", list(script))
        self.client = LLMClient(
            mcfg,
            {"a": self.provider},
            LLMCache(":memory:"),
            UsageLedger(":memory:", mcfg.catalog, 1.0, 0),
        )
        self.sent: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.sent.append(
                {"headers": dict(request.headers), "body": json.loads(request.content)}
            )
            return httpx.Response(200)

        self.publisher = EventPublisher(
            "http://n8n.test/webhook",
            "secret",
            synchronous=True,
            transport=httpx.MockTransport(handler),
        )
        options = RuntimeOptions(
            demo_clock=True,
            register_scenarios=False,
            ops_path=tmp / "ops.sqlite3",
            checkpoint_path=tmp / "cp.sqlite3",
            actions_path=tmp / "a.sqlite3",
            history_path=tmp / "h.sqlite3",
        )
        self.runtime = Runtime(
            cfg,
            self.client,
            engine,
            judge or (lambda rt: CodeChecksOnlyJudge()),
            options=options,
            publisher=self.publisher,
            triage=Triage(None),
        )
        self.runtime.register_booking(
            Booking(
                booking_ref="ABC123",
                passenger_ref="P1",
                passenger_name="Anna Schmidt",
                fare_eur=250,
                case=case(),
            ),
            NOW,
        )
        self.http = TestClient(create_app(self.runtime, KEY, ADMIN))

    def conversation(self) -> str:
        r = self.http.post("/conversations", json={"channel": "webhook", "booking_ref": "ABC123"})
        assert r.status_code == 201
        return str(r.json()["conversation_id"])

    def say(self, cid: str, text: str, key: str) -> dict[str, Any]:
        r = self.http.post(
            f"/conversations/{cid}/messages", json={"text": text, "idempotency_key": key}
        )
        assert r.status_code == 200, r.text
        return dict(r.json())


@pytest.fixture
def make(engine: EntitlementEngine, tmp_path: Path):  # type: ignore[no-untyped-def]
    def _make(script: list[str], judge: Any = None) -> Setup:
        return Setup(engine, tmp_path, script, judge)

    return _make


def test_healthz_reports_versions(make) -> None:  # type: ignore[no-untyped-def]
    body = make([]).http.get("/healthz").json()
    assert body["status"] == "ok" and body["rulesets"]["eu261_amended_2027"] == "2027.1"
    assert body["rubric_id"] == "code_checks_only"


def test_FR_1_1_customer_gets_the_final_reply_only(make) -> None:  # type: ignore[no-untyped-def]
    s = make([GOOD_DRAFT])
    cid = s.conversation()
    out = s.say(cid, "My flight is delayed, help", "m1")
    assert out == {
        "reply_text": GOOD_DRAFT,
        "status": "answered",
        "ticket_id": None,
        "duplicate": False,
    }


def test_FR_1_4_duplicate_delivery(make) -> None:  # type: ignore[no-untyped-def]
    s = make([GOOD_DRAFT])
    cid = s.conversation()
    first, again = s.say(cid, "Delayed", "m1"), s.say(cid, "Delayed", "m1")
    assert (
        again["duplicate"]
        and again["reply_text"] == first["reply_text"]
        and len(s.provider.requests) == 1
    )


def test_problem_json_errors(make) -> None:  # type: ignore[no-untyped-def]
    s = make([])
    r = s.http.post("/conversations/C-nope/messages", json={"text": "hi", "idempotency_key": "k"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    bad = s.http.post("/conversations/C-nope/messages", json={"text": ""})
    assert bad.status_code == 422 and bad.json()["title"] == "invalid request"


def test_NFR_5_ops_endpoints_need_the_api_key(make) -> None:  # type: ignore[no-untyped-def]
    s = make([])
    assert s.http.get("/ops/judgements").status_code == 401
    assert s.http.get("/ops/judgements", headers={"X-API-Key": "wrong"}).status_code == 401
    assert s.http.get("/ops/judgements", headers=OPS).status_code == 200


def test_NFR_6_pii_is_masked_unless_admin_unmasks(make) -> None:  # type: ignore[no-untyped-def]
    s = make([GOOD_DRAFT])
    cid = s.conversation()
    s.say(cid, "Hi, I am Anna Schmidt, booking ABC123, my flight is delayed", "m1")
    masked = s.http.get(f"/ops/conversations/{cid}", headers=OPS).json()
    text = json.dumps(masked)
    assert "ABC123" not in text and "Schmidt" not in text and "AB****" in text
    assert s.http.get(f"/ops/conversations/{cid}?unmask=true", headers=OPS).status_code == 403
    full = s.http.get(
        f"/ops/conversations/{cid}?unmask=true", headers={**OPS, "X-Admin-Key": ADMIN}
    ).json()
    assert (
        "ABC123" in json.dumps(full)
        and full["triage"]["tier"]
        and full["entitlement"]["compensation_eur"] == 400
    )


def test_FR_11_judgements_are_queryable_with_filters_and_diff(make) -> None:  # type: ignore[no-untyped-def]
    s = make([GOOD_DRAFT.replace("EUR 400", "EUR 600"), GOOD_DRAFT])
    cid = s.conversation()
    s.say(cid, "Delayed, help", "m1")
    rows = s.http.get(f"/ops/conversations/{cid}/judgements", headers=OPS).json()
    assert [r["decision"] for r in rows] == [
        "approve",
        "approve",
    ]  # placeholder judge; code checks force revise
    listing = s.http.get("/ops/judgements?decision=approve&limit=1", headers=OPS).json()
    assert len(listing["items"]) == 1 and listing["next_cursor"]
    first = rows[0]["judgement_id"]
    detail = s.http.get(f"/ops/judgements/{first}", headers=OPS).json()
    assert detail["attempt"]["attempt_no"] == 1 and not detail["attempt"]["checks"][0]["passed"]
    diff = s.http.get(f"/ops/judgements/{first}/diff", headers=OPS).json()
    assert "EUR 600" in diff["failed_draft"] and "EUR 400" in diff["revision"]
    assert diff["failed_checks"][0]["check_id"] == "amounts_match_entitlement"
    assert s.http.get("/ops/judgements/J-nope", headers=OPS).status_code == 404


def test_FR_9_handoff_lifecycle_and_signed_event(make) -> None:  # type: ignore[no-untyped-def]
    s = make([SUMMARY])
    cid = s.conversation()
    out = s.say(cid, "I want to speak to a human agent about my delayed flight", "m1")
    ticket = out["ticket_id"]
    assert out["status"] == "handed_off" and ticket
    sent = [m for m in s.sent if m["body"]["event"] == "handoff.created"]
    payload = sent[0]["body"]["payload"]
    assert payload["conversation_id"] == cid and payload["ticket_id"] == ticket
    assert payload["sla_minutes"] == {"S1": 240, "S2": 60, "S3": 15, "S4": 10}[payload["tier"]]
    h = sent[0]["headers"]
    assert verify(
        "secret", h["x-timestamp"], json.dumps(sent[0]["body"]).encode(), h["x-signature"]
    )
    listed = s.http.get("/ops/handoffs?status=open", headers=OPS).json()
    assert (
        listed[0]["ticket_id"] == ticket and listed[0]["sla_breached"] is False
    )  # demo clock is 2027
    assert (
        s.http.post(f"/ops/handoffs/{ticket}/acknowledge", headers=OPS).json()["status"]
        == "acknowledged"
    )
    assert s.http.post(f"/ops/handoffs/{ticket}/escalate", headers=OPS).status_code == 409
    done = s.http.post(
        f"/ops/handoffs/{ticket}/resolve",
        headers=OPS,
        json={"decision": "compensation approved", "notes": "paid", "compensation_paid": 400},
    ).json()
    assert done["status"] == "resolved" and done["resolution"]["compensation_paid_eur"] == 400
    memory = s.runtime.history.get("P1")
    assert memory.resolutions[0].compensation_paid_eur == 400
    assert (
        s.http.post("/ops/handoffs/T-nope/resolve", headers=OPS, json={"decision": "x"}).status_code
        == 404
    )
    assert s.http.get(f"/ops/handoffs/{ticket}", headers=OPS).json()["status"] == "resolved"


def test_sla_breached_and_escalation(make) -> None:  # type: ignore[no-untyped-def]
    s = make([SUMMARY])
    cid = s.conversation()
    ticket = s.say(cid, "Please let me talk to a real person about the flight", "m1")["ticket_id"]
    with s.runtime.store._db:  # make it overdue
        s.runtime.store._db.execute("UPDATE handoffs SET sla_due_at = '2020-01-01T00:00:00+00:00'")
    assert [
        h["ticket_id"] for h in s.http.get("/ops/handoffs?sla_breached=true", headers=OPS).json()
    ] == [ticket]
    assert (
        s.http.post(f"/ops/handoffs/{ticket}/escalate", headers=OPS).json()["status"] == "escalated"
    )


def test_FR_17_sse_replays_and_global_judgement_stream(make) -> None:  # type: ignore[no-untyped-def]
    s = make([GOOD_DRAFT])
    cid = s.conversation()
    s.say(cid, "Delayed", "m1")
    body = s.http.get(f"/ops/conversations/{cid}/stream?once=true", headers=OPS).text
    for name in (
        "triage.completed",
        "entitlement.computed",
        "draft.created",
        "checks.completed",
        "judgement.completed",
        "reply.sent",
    ):
        assert f"event: {name}" in body
    only = s.http.get("/ops/judgements/stream?once=true", headers=OPS).text
    assert "event: judgement.completed" in only and "event: draft.created" not in only


def test_broker_fans_out_live_events(make) -> None:  # type: ignore[no-untyped-def]
    s = make([GOOD_DRAFT])
    everything = s.runtime.broker.subscribe(None)
    cid = s.conversation()
    mine = s.runtime.broker.subscribe(cid)
    s.say(cid, "Delayed", "m1")
    names = [mine.get_nowait()["event"] for _ in range(mine.qsize())]
    assert (
        names[0] == "triage.completed"
        and names[-1] == "reply.sent"
        and everything.qsize() == len(names)
    )
    s.runtime.broker.unsubscribe(cid, mine)


def judged_setup(make) -> tuple[Setup, str, str]:  # type: ignore[no-untyped-def]
    """A conversation judged by the real (scripted) SeverityAwareJudge: judgements carry scores."""
    quote = "I'm sorry your flight is delayed"
    dims = [
        "relevance",
        "context_awareness",
        "urgency",
        "tone",
        "grievance_handling",
        "correctness",
        "actionability",
        "fairness_safety",
    ]
    answers = [
        {
            "id": d,
            **(
                {"passed": True}
                if d in ("urgency", "correctness", "fairness_safety")
                else {"score": 4}
            ),
            "evidence": quote,
        }
        for d in dims
    ]
    verdict = json.dumps({"answers": answers, "label": "good"})
    s = make(
        [GOOD_DRAFT, verdict],
        judge=lambda rt: SeverityAwareJudge(rt.client, exemplars=rt.exemplars),
    )
    cid = s.conversation()
    s.say(cid, "Delayed, help", "m1")
    jid = s.http.get(f"/ops/conversations/{cid}/judgements", headers=OPS).json()[0]["judgement_id"]
    return s, cid, jid


def test_FR_16_corrections_examples_proposals_and_apply(make) -> None:  # type: ignore[no-untyped-def]
    s, _, jid = judged_setup(make)
    assert (
        s.http.post(
            f"/ops/judgements/{jid}/corrections",
            headers=OPS,
            json={"split": "test", "dimension_overrides": {"tone": 2}},
        ).status_code
        == 400
    )
    assert (
        s.http.post(
            f"/ops/judgements/{jid}/corrections",
            headers=OPS,
            json={"dimension_overrides": {"nonsense": 2}},
        ).status_code
        == 422
    )
    new = []
    for _ in range(3):
        r = s.http.post(
            f"/ops/judgements/{jid}/corrections",
            headers=OPS,
            json={"split": "dev", "dimension_overrides": {"tone": 2}, "reason": "too generic"},
        )
        assert r.status_code == 201
        new += r.json()["new_proposals"]
    assert len(new) == 1
    proposals = s.http.get("/ops/judge/proposals?status=open", headers=OPS).json()
    assert (
        proposals[0]["dimension"] == "tone"
        and proposals[0]["change"]["action"] == "raise_min_score"
    )
    applied = s.http.post(f"/ops/judge/proposals/{new[0]}/apply", headers=OPS).json()
    assert applied["rubric_version"] == "1.0+p1"
    assert s.runtime.judge.rubric.profile("S2").min_scores["tone"] == 4  # type: ignore[attr-defined]
    assert s.http.post(f"/ops/judge/proposals/{new[0]}/apply", headers=OPS).status_code == 409
    assert s.http.get("/healthz").json()["rubric_id"] == "airline_v1@1.0+p1"
    assert (
        len(s.http.get("/ops/judge/corrections?dimension=tone&split=dev", headers=OPS).json()) == 3
    )
    ex = s.http.post(
        "/ops/judge/examples", headers=OPS, json={"prompt_text": "p", "expected_reply": "r"}
    )
    assert ex.status_code == 201 and len(s.http.get("/ops/judge/examples", headers=OPS).json()) == 1
    assert (
        s.http.post(
            "/ops/judge/examples",
            headers=OPS,
            json={"prompt_text": "p", "expected_reply": "r", "split": "test"},
        ).status_code
        == 400
    )
    exemplars = s.runtime.exemplars("S2", "reply")
    assert exemplars and exemplars[0].dimension == "tone" and exemplars[0].score == 2


def test_FR_17_metrics_and_quality_signals(make) -> None:  # type: ignore[no-untyped-def]
    s, _, _ = judged_setup(make)
    m = s.http.get("/ops/metrics/judge?window=all", headers=OPS).json()
    assert (
        m["judgements"] == 1 and m["decisions_by_tier"] and m["dimension_pass_rate"]["tone"] == 1.0
    )
    assert (
        m["quota_today"]
        and s.http.get("/ops/metrics/judge?window=1y", headers=OPS).status_code == 422
    )
    assert s.http.get("/ops/metrics/fairness", headers=OPS).json()["available"] in (True, False)


def test_FR_17_5_threshold_alert_is_sent_once(make) -> None:  # type: ignore[no-untyped-def]
    s = make([])
    store = s.runtime.store
    now = datetime.now(UTC)
    for i in range(12):
        rec = {
            "target": "reply",
            "decision": "handoff",
            "error": "down",
            "scores": [],
            "failures": [],
        }
        with store._db:
            store._db.execute(
                "INSERT INTO judgements VALUES "
                "(?, 'C', 1, NULL, 'reply', 'S2', 'bad', 'handoff', '', ?, ?)",
                (f"J{i}", json.dumps(rec), (now - timedelta(minutes=5)).isoformat()),
            )
    first = s.runtime.check_alerts()
    assert "judge_unavailable_rate" in first and "handoff_rate" in first
    assert s.runtime.check_alerts() == []  # deduplicated within the hour
    assert any(m["body"]["event"] == "metrics.threshold_breached" for m in s.sent)


def test_FR_17_5_alert_check_endpoint(make) -> None:  # type: ignore[no-untyped-def]
    s = make([])
    r = s.http.post("/ops/alerts/check", headers=OPS)
    assert r.status_code == 200 and r.json() == {"breached": []}  # below min_judgements
    assert s.http.post("/ops/alerts/check").status_code == 401


def test_notifications_log(make) -> None:  # type: ignore[no-untyped-def]
    s = make([])
    r = s.http.post(
        "/ops/notifications",
        headers=OPS,
        json={"kind": "sla.escalated", "ticket_id": "T1", "text": "Ticket T1 escalated"},
    )
    assert r.status_code == 201
    rows = s.http.get("/ops/notifications?kind=sla.escalated", headers=OPS).json()
    assert (
        rows[0]["ticket_id"] == "T1"
        and s.http.get("/ops/notifications?ticket_id=T9", headers=OPS).json() == []
    )


def test_booking_registration_endpoint(make) -> None:  # type: ignore[no-untyped-def]
    s = make([])
    booking = Booking(
        booking_ref="ZZZ999", passenger_ref="P9", passenger_name="Joao Silva", case=case()
    )
    r = s.http.post(
        "/ops/bookings",
        headers=OPS,
        json={"booking": json.loads(booking.model_dump_json()), "demo_clock": NOW.isoformat()},
    )
    assert r.status_code == 201 and s.runtime.airline.get_booking("ZZZ999") is not None


def test_signatures_and_replay_window() -> None:
    body = b'{"a": 1}'
    sig = sign("s", "1000", body)
    assert verify("s", "1000", body, sig, now=1100)
    assert not verify("s", "1000", body, sig, now=2000)  # outside the 5 min window
    assert not verify("s", "1000", b"tampered", sig, now=1100) and not verify("s", "x", body, sig)


def test_publisher_without_url_is_a_no_op_and_retries_on_server_errors() -> None:
    assert EventPublisher(None, "s").publish("x", {})["event"] == "x"
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    p = EventPublisher(
        "http://n8n.test", "s", synchronous=True, retries=2, transport=httpx.MockTransport(flaky)
    )
    p.publish("handoff.created", {})
    assert len(calls) == 2 and len(p.failed) == 1 and not p.sent
