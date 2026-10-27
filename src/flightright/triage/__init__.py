"""Severity triage (FR-3): rules + LLM, higher tier wins."""

from flightright.triage.rules import TriageContext
from flightright.triage.triage import Triage

__all__ = ["Triage", "TriageContext"]
