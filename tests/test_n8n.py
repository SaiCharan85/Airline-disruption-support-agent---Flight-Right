"""Exported n8n workflows (SPEC 8.1, 8.3): no embedded secrets, valid structure, API wiring."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from flightright.n8n import find_secrets, lint, load_all, normalize, webhook_paths

WORKFLOWS = load_all()


def nodes_of(type_: str) -> list[tuple[str, dict[str, Any]]]:
    return [(f, n) for f, wf in WORKFLOWS.items() for n in wf["nodes"] if n["type"] == type_]


def test_FR_12_all_five_workflows_exported() -> None:
    names = sorted(wf["name"].split()[0] for wf in WORKFLOWS.values())
    assert names == ["W1", "W2", "W3", "W4", "W5"]


@pytest.mark.parametrize("filename", sorted(WORKFLOWS))
def test_FR_12_exported_workflow_has_no_secrets_and_lints(filename: str) -> None:
    wf = WORKFLOWS[filename]
    assert find_secrets(wf) == []
    assert lint(wf) == []
    assert normalize(wf) == wf  # no volatile export fields committed


def test_FR_12_webhook_paths_unique_and_expected() -> None:
    paths = webhook_paths(list(WORKFLOWS.values()))
    assert len(paths) == len(set(paths))
    for expected in (
        "webhook/inbound",
        "webhook/flightright-events",
        "webhook/handoff-action-api",
        "form/handoff-action",
        "webhook/digest-now",
    ):
        assert expected in paths


def test_FR_12_http_nodes_use_env_api_url_and_ops_key() -> None:
    for filename, node in nodes_of("n8n-nodes-base.httpRequest"):
        url = node["parameters"]["url"]
        assert url.startswith("={{ $env.FLIGHTRIGHT_API_URL }}/"), (filename, node["name"])
        if "/ops/" in url:
            headers = node["parameters"]["headerParameters"]["parameters"]
            assert {"name": "X-API-Key", "value": "={{ $env.OPS_API_KEY }}"} in headers


def test_FR_12_code_nodes_do_no_reasoning_or_hidden_calls() -> None:
    """SPEC 8.1: Code nodes format, map and verify signatures; no HTTP or model calls."""
    for filename, node in nodes_of("n8n-nodes-base.code"):
        js = node["parameters"]["jsCode"]
        assert "httpRequest" not in js and "fetch(" not in js, (filename, node["name"])
        assert js.lstrip().startswith("//") or "const" in js


def test_NFR_5_events_webhook_verifies_hmac_over_raw_body() -> None:
    wf = next(w for w in WORKFLOWS.values() if w["name"].startswith("W2"))
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert hook["parameters"]["options"]["rawBody"] is True
    verify = next(n for n in wf["nodes"] if n["name"] == "Verify signature")["parameters"]["jsCode"]
    for needle in ("createHmac('sha256'", "timingSafeEqual", "> 300", "duplicate event_id"):
        assert needle in verify


def test_find_secrets_catches_planted_leaks() -> None:
    wf = copy.deepcopy(next(iter(WORKFLOWS.values())))
    node = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest")
    node["parameters"]["headerParameters"] = {
        "parameters": [{"name": "X-API-Key", "value": "literal-key-value-1234"}]
    }
    node["credentials"] = {"httpHeaderAuth": {"id": "1", "name": "ops"}}
    node["parameters"]["jsonBody"] = "gsk_" + "a" * 40
    problems = find_secrets(wf, env_values=["literal-key-value-1234"])
    joined = "\n".join(problems)
    for expected in ("stored credentials", "not read from $env", "Groq key", "value from .env"):
        assert expected in joined


def test_lint_catches_broken_connection() -> None:
    wf = copy.deepcopy(next(iter(WORKFLOWS.values())))
    wf["connections"]["Ghost"] = {"main": [[{"node": "Nowhere", "type": "main", "index": 0}]]}
    assert any("unknown node" in p for p in lint(wf))
