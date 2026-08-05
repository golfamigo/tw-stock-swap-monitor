"""Explicit registry for indicator plugins without strategy-core modification."""

from app.indicators.base import Indicator, IndicatorError


class IndicatorRegistrationError(IndicatorError):
    """A plugin does not satisfy the registration contract."""


class DuplicateIndicatorKeyError(IndicatorRegistrationError):
    """An already-registered key was offered by another plugin."""


class IndicatorNotFoundError(IndicatorError):
    """No registered plugin exists for the requested key."""


class IndicatorRegistry:
    """Own plugin registration and lookup at the application composition boundary."""

    def __init__(self) -> None:
        self._plugins: dict[str, Indicator] = {}

    def register(self, plugin: Indicator) -> None:
        """Register one protocol-conforming plugin under its non-blank unique key."""

        if not isinstance(plugin, Indicator):
            raise IndicatorRegistrationError("plugin must implement the Indicator protocol")
        if not callable(plugin.calculate):
            raise IndicatorRegistrationError("plugin calculate member must be callable")
        key = plugin.key
        if not isinstance(key, str) or not key.strip():
            raise IndicatorRegistrationError("plugin key must be a non-blank string")
        normalized_key = key.strip()
        if normalized_key in self._plugins:
            raise DuplicateIndicatorKeyError(
                f"indicator key is already registered: {normalized_key}"
            )
        self._plugins[normalized_key] = plugin

    def get(self, key: str) -> Indicator:
        """Resolve one plugin or raise a typed error that names the missing key."""

        if not isinstance(key, str) or not key.strip():
            raise IndicatorNotFoundError("indicator key must be a non-blank string")
        normalized_key = key.strip()
        try:
            return self._plugins[normalized_key]
        except KeyError as error:
            raise IndicatorNotFoundError(
                f"indicator is not registered: {normalized_key}"
            ) from error

    def keys(self) -> tuple[str, ...]:
        """Return registered keys in deterministic order for configuration inspection."""

        return tuple(sorted(self._plugins))
