"""Typed view of config/app_config.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str | None = None


class ModelLimits(BaseModel):
    family: str
    efforts: list[str] = Field(default_factory=list)
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    otpm: int | None = None  # output tokens per minute (Groq refuses larger max_tokens)
    thinking_tokens: int = 0  # added on top of the role cap for models that always think first
    tpm_input_only: bool = False  # the provider's per-minute token limit counts input tokens only


class RoleConfig(BaseModel):
    chain: list[str]
    max_tokens: int = 800
    reasoning_effort: str | None = None


class ModelsConfig(BaseModel):
    safety_margin: float = 0.9
    day_reset_utc_offset_hours: int = -8
    timeout_s: float = 90
    cache_path: str = ".cache/llm/cache.sqlite3"
    usage_path: str = ".cache/llm/usage.sqlite3"
    providers: dict[str, ProviderConfig]
    catalog: dict[str, ModelLimits]
    roles: dict[str, RoleConfig]


class AuthorityConfig(BaseModel):
    care_voucher_max_eur: dict[str, int]
    care_voucher_standard_eur: dict[str, int] = Field(default_factory=dict)
    care_vouchers_max_total_eur: int
    refund_auto_max_eur: int
    compensation_auto_initiate: bool = False


class StoresConfig(BaseModel):
    history_path: str
    actions_path: str


class AgentConfig(BaseModel):
    max_revisions: int = 2
    abuse_handoff_threshold: int = 2
    reply_max_words: int = 220
    checkpoint_path: str = ".cache/stores/checkpoints.sqlite3"


class AppConfig(BaseModel):
    seed: int
    models: ModelsConfig
    authority: AuthorityConfig
    stores: StoresConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sla_minutes: dict[str, int] = Field(
        default_factory=lambda: {"S1": 240, "S2": 60, "S3": 15, "S4": 10}
    )


def load_env(path: Path | None = None) -> None:
    """Read KEY=value lines from .env into os.environ (existing variables win)."""
    import os
    import re

    env = path or ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match and match.group(2) and not os.environ.get(match.group(1)):
            os.environ[match.group(1)] = match.group(2)


def load_config(path: Path | None = None) -> AppConfig:
    raw = yaml.safe_load((path or ROOT / "config" / "app_config.yaml").read_text(encoding="utf-8"))
    return AppConfig(
        seed=raw["harness"]["seed"],
        models=ModelsConfig.model_validate(raw["models"]),
        authority=AuthorityConfig.model_validate(raw["authority"]),
        stores=StoresConfig.model_validate(raw["stores"]),
        agent=AgentConfig.model_validate(raw.get("agent", {})),
        sla_minutes=raw.get("sla_minutes", {"S1": 240, "S2": 60, "S3": 15, "S4": 10}),
    )
