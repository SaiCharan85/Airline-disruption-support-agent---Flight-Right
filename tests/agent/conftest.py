from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from flightright.agent.testing import Harness, build_harness
from flightright.rules.engine import EntitlementEngine


@pytest.fixture
def make(engine: EntitlementEngine) -> Callable[..., Harness]:
    def _make(script: list[Any], **kw: Any) -> Harness:
        return build_harness(engine, script, **kw)

    return _make
