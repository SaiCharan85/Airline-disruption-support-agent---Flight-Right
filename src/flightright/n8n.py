"""Checks for the exported n8n workflows (SPEC 8.1, 8.3).

Exported JSON must carry no credentials or secrets: every key, token and URL comes from `$env`
(set from .env by docker compose). The structural lint catches broken connections and duplicate
webhook paths before an import does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flightright.config import ROOT

WORKFLOW_DIR = ROOT / "n8n" / "workflows"

# Volatile fields n8n adds on export; dropped so diffs only show real changes. staticData is
# runtime state (W1's channel-user -> conversation map, seen event IDs) and must never be committed.
VOLATILE = (
    "staticData",
    "nodeGroups",
    "sourceWorkflowId",
    "versionMetadata",
    "createdAt",
    "updatedAt",
    "versionId",
    "activeVersionId",
    "activeVersion",
    "versionCounter",
    "triggerCount",
    "shared",
    "meta",
    "isArchived",
    "parentFolder",
    "description",
)

SECRET_PATTERNS = {
    "Groq key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "LangSmith key": re.compile(r"lsv2_[A-Za-z0-9_]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "Slack webhook": re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SENSITIVE_HEADERS = {"x-api-key", "x-admin-key", "authorization", "x-signature"}


def _walk(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(obj, dict):
        return [kv for k, v in obj.items() for kv in _walk(v, f"{path}.{k}" if path else k)]
    if isinstance(obj, list):
        return [kv for i, v in enumerate(obj) for kv in _walk(v, f"{path}[{i}]")]
    return [(path, obj)]


def find_secrets(workflow: dict[str, Any], env_values: list[str] | None = None) -> list[str]:
    """Problems that would leak a secret if this workflow were committed."""
    problems = []
    for node in workflow.get("nodes", []):
        if node.get("credentials"):
            problems.append(f"{node['name']}: has stored credentials attached")
        for header in node.get("parameters", {}).get("headerParameters", {}).get("parameters", []):
            name, value = str(header.get("name", "")).lower(), str(header.get("value", ""))
            if name in SENSITIVE_HEADERS and "$env." not in value:
                problems.append(f"{node['name']}: header {header['name']} is not read from $env")
    for path, value in _walk(workflow):
        if not isinstance(value, str):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(value):
                problems.append(f"{path}: looks like a {label}")
        for secret in env_values or []:
            if len(secret) >= 12 and secret in value:
                problems.append(f"{path}: contains a value from .env")
    return problems


def lint(workflow: dict[str, Any]) -> list[str]:
    """Structural problems: connections to missing nodes, missing ids, duplicate names."""
    problems = []
    names = [n["name"] for n in workflow.get("nodes", [])]
    if len(names) != len(set(names)):
        problems.append("duplicate node names")
    for key in ("id", "name", "nodes", "connections"):
        if key not in workflow:
            problems.append(f"missing {key}")
    for src, outputs in workflow.get("connections", {}).items():
        if src not in names:
            problems.append(f"connection from unknown node {src}")
        for branch in outputs.get("main", []):
            for target in branch or []:
                if target["node"] not in names:
                    problems.append(f"{src} -> unknown node {target['node']}")
    return problems


def webhook_paths(workflows: list[dict[str, Any]]) -> list[str]:
    paths = []
    for wf in workflows:
        for node in wf.get("nodes", []):
            if node["type"] in ("n8n-nodes-base.webhook", "n8n-nodes-base.formTrigger"):
                kind = "form" if node["type"].endswith("formTrigger") else "webhook"
                params = node["parameters"]
                path = params.get("options", {}).get("path") if kind == "form" else None
                paths.append(f"{kind}/{path or params.get('path', '')}")
    return paths


def normalize(workflow: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in workflow.items() if k not in VOLATILE}


def load_all(directory: Path = WORKFLOW_DIR) -> dict[str, dict[str, Any]]:
    return {
        p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))
    }


def env_secret_values(path: Path = ROOT / ".env") -> list[str]:
    """Values of secret-looking keys in .env (never printed; used only for leak checks)."""
    if not path.is_file():
        return []
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if re.search(r"KEY|SECRET|TOKEN|PASSWORD", key) and value.strip():
            values.append(value.strip().strip('"'))
    return values
