"""Plugin registry behavior isolated from strategy code."""

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

import pytest
from app.data_sources.models import MarketDataSnapshot
from app.indicators.base import Indicator, IndicatorResult
from app.indicators.registry import (
    DuplicateIndicatorKeyError,
    IndicatorNotFoundError,
    IndicatorRegistrationError,
    IndicatorRegistry,
)


@dataclass(frozen=True, slots=True)
class ExampleIndicator:
    key: str = "example"

    def calculate(self, market_snapshot: object) -> IndicatorResult:
        return IndicatorResult.available(key=self.key, value=Decimal("1"), evidence={})


@dataclass(frozen=True, slots=True)
class BlankKeyIndicator:
    """Structurally valid plugin rejected by runtime registration validation."""

    key: str = " "

    def calculate(self, market_snapshot: MarketDataSnapshot) -> IndicatorResult:
        return IndicatorResult.available(key="placeholder", value=Decimal("1"), evidence={})


@dataclass(frozen=True, slots=True)
class NonCallableCalculatePlugin:
    """A runtime-invalid structural object whose calculate member is not callable."""

    key: str = "non-callable"
    calculate: str = "not-a-function"


def test_registers_and_resolves_indicator_plugins_without_strategy_core_changes() -> None:
    registry = IndicatorRegistry()
    plugin = ExampleIndicator()

    registry.register(plugin)

    assert registry.get("example") is plugin
    assert registry.keys() == ("example",)


def test_registry_rejects_duplicate_missing_and_invalid_plugins() -> None:
    registry = IndicatorRegistry()
    registry.register(ExampleIndicator())

    with pytest.raises(DuplicateIndicatorKeyError):
        registry.register(ExampleIndicator())
    with pytest.raises(IndicatorNotFoundError):
        registry.get("unknown")
    with pytest.raises(IndicatorRegistrationError):
        registry.register(BlankKeyIndicator())
    with pytest.raises(IndicatorRegistrationError, match="callable"):
        registry.register(cast(Indicator, NonCallableCalculatePlugin()))
