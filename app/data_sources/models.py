"""Immutable, framework-free market-data request and evidence types."""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from app.domain.entities import Instrument
from app.domain.values import require_finite_decimal, require_timezone_aware

_REQUIRED_OHLCV_FIELDS = frozenset({"open", "high", "low", "close", "volume"})


def canonical_datetime(value: datetime) -> str:
    """Encode one aware timestamp at UTC microsecond precision for shared evidence."""

    require_timezone_aware(value, field_name="datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    """Encode finite Decimals without a scale or signed-zero ambiguity."""

    require_finite_decimal(value, field_name="decimal")
    if value.is_zero():
        return "0"
    return _exact_decimal_text(value)


def _exact_decimal_text(value: Decimal) -> str:
    """Encode finite nonzero Decimals without context-dependent normalization."""

    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise AssertionError("finite Decimal must have an integer exponent")
    digits = "".join(str(digit) for digit in decimal_tuple.digits).rstrip("0")
    exponent = decimal_tuple.exponent + (len(decimal_tuple.digits) - len(digits))
    sign = "-" if decimal_tuple.sign else ""
    if exponent >= 0:
        return f"{sign}{digits}{'0' * exponent}"
    decimal_point = len(digits) + exponent
    if decimal_point > 0:
        return f"{sign}{digits[:decimal_point]}.{digits[decimal_point:]}"
    return f"{sign}0.{('0' * -decimal_point)}{digits}"


def _timedelta_text(value: timedelta) -> str:
    """Encode a duration exactly, without passing through floating-point seconds."""

    total_microseconds = (
        value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    )
    return f"{total_microseconds}us"


def _canonical_json(payload: dict[str, object]) -> str:
    """Produce the stable JSON representation signed by a market-data snapshot."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class MarketDataRequest:
    """Deterministic quote/intraday input and daily dates in [daily_start, daily_end)."""

    request_id: UUID
    instruments: tuple[Instrument, ...]
    requested_at: datetime
    intraday_start: datetime
    intraday_end: datetime
    intraday_interval: timedelta
    daily_start: date
    daily_end: date
    maximum_data_delay: timedelta

    def __post_init__(self) -> None:
        instruments = tuple(self.instruments)
        if not instruments:
            raise ValueError("instruments must not be empty")
        if any(not isinstance(instrument, Instrument) for instrument in instruments):
            raise TypeError("instruments must contain Instrument values")
        if len({instrument.instrument_id for instrument in instruments}) != len(instruments):
            raise ValueError("instruments must be unique")
        require_timezone_aware(self.requested_at, field_name="requested_at")
        require_timezone_aware(self.intraday_start, field_name="intraday_start")
        require_timezone_aware(self.intraday_end, field_name="intraday_end")
        if self.intraday_end <= self.intraday_start:
            raise ValueError("intraday_end must be after intraday_start")
        if self.intraday_interval <= timedelta():
            raise ValueError("intraday_interval must be positive")
        if self.daily_end <= self.daily_start:
            raise ValueError("daily_end must be after daily_start")
        if self.maximum_data_delay < timedelta():
            raise ValueError("maximum_data_delay must not be negative")
        object.__setattr__(
            self,
            "instruments",
            tuple(sorted(instruments, key=lambda instrument: str(instrument.instrument_id))),
        )

    def canonical_payload(self) -> dict[str, object]:
        """Return every request input used to distinguish immutable snapshot evidence."""

        return {
            "daily_end": self.daily_end.isoformat(),
            "daily_start": self.daily_start.isoformat(),
            "instruments": [_instrument_payload(instrument) for instrument in self.instruments],
            "intraday_end": canonical_datetime(self.intraday_end),
            "intraday_interval": _timedelta_text(self.intraday_interval),
            "intraday_start": canonical_datetime(self.intraday_start),
            "maximum_data_delay": _timedelta_text(self.maximum_data_delay),
            "request_id": str(self.request_id),
            "requested_at": canonical_datetime(self.requested_at),
        }


@dataclass(frozen=True, slots=True)
class DataQuality:
    """Explicit provider freshness and completeness evidence used to gate actions."""

    provider: str
    fetched_at: datetime
    source_timestamp: datetime
    delay: timedelta
    stale: bool
    missing_fields: tuple[str, ...]
    bars_complete: bool
    anomalies: tuple[str, ...]
    confidence: Decimal

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be blank")
        require_timezone_aware(self.fetched_at, field_name="fetched_at")
        require_timezone_aware(self.source_timestamp, field_name="source_timestamp")
        if self.delay < timedelta():
            raise ValueError("delay must not be negative")
        elapsed_delay = self.fetched_at.astimezone(UTC) - self.source_timestamp.astimezone(UTC)
        if self.delay != elapsed_delay:
            raise ValueError("delay must equal fetched_at minus source_timestamp")
        missing_fields = _normalized_labels(self.missing_fields, field_name="missing_fields")
        anomalies = _normalized_labels(self.anomalies, field_name="anomalies")
        require_finite_decimal(self.confidence, field_name="confidence")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(self, "missing_fields", missing_fields)
        object.__setattr__(self, "anomalies", anomalies)

    @property
    def is_actionable(self) -> bool:
        """Return false for stale, incomplete, or required-OHLCV-missing evidence."""

        return (
            not self.stale
            and self.bars_complete
            and not self.anomalies
            and not (_REQUIRED_OHLCV_FIELDS & set(self.missing_fields))
        )

    def canonical_payload(self) -> dict[str, object]:
        """Return the complete quality evidence preserved in the snapshot hash."""

        return {
            "anomalies": list(self.anomalies),
            "bars_complete": self.bars_complete,
            "confidence": _decimal_text(self.confidence),
            "delay": _timedelta_text(self.delay),
            "fetched_at": canonical_datetime(self.fetched_at),
            "missing_fields": list(self.missing_fields),
            "provider": self.provider,
            "source_timestamp": canonical_datetime(self.source_timestamp),
            "stale": self.stale,
        }


@dataclass(frozen=True, slots=True)
class Quote:
    """A finite point-in-time price for one requested instrument."""

    instrument_id: UUID
    as_of: datetime
    price: Decimal

    def __post_init__(self) -> None:
        require_timezone_aware(self.as_of, field_name="as_of")
        require_finite_decimal(self.price, field_name="price")
        if self.price <= Decimal("0"):
            raise ValueError("price must be positive")

    def canonical_payload(self) -> dict[str, str]:
        """Return quote evidence suitable for canonical snapshot hashing."""

        return {
            "as_of": canonical_datetime(self.as_of),
            "instrument_id": str(self.instrument_id),
            "price": _decimal_text(self.price),
        }


@dataclass(frozen=True, slots=True)
class Bar:
    """An OHLCV observation over a left-closed, right-open time interval."""

    instrument_id: UUID
    starts_at: datetime
    ends_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        require_timezone_aware(self.starts_at, field_name="starts_at")
        require_timezone_aware(self.ends_at, field_name="ends_at")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        for field_name in ("open", "high", "low", "close", "volume"):
            require_finite_decimal(getattr(self, field_name), field_name=field_name)
        if self.open <= Decimal("0") or self.close <= Decimal("0"):
            raise ValueError("open and close must be positive")
        if self.high < max(self.open, self.close):
            raise ValueError("high must not be below open or close")
        if self.low > min(self.open, self.close):
            raise ValueError("low must not exceed open or close")
        if self.volume < Decimal("0"):
            raise ValueError("volume must not be negative")

    def canonical_payload(self) -> dict[str, str]:
        """Return complete OHLCV evidence with no missing-value substitution."""

        return {
            "close": _decimal_text(self.close),
            "ends_at": canonical_datetime(self.ends_at),
            "high": _decimal_text(self.high),
            "instrument_id": str(self.instrument_id),
            "low": _decimal_text(self.low),
            "open": _decimal_text(self.open),
            "starts_at": canonical_datetime(self.starts_at),
            "volume": _decimal_text(self.volume),
        }


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    """Canonical result evidence returned by every market-data provider operation."""

    provider: str
    request: MarketDataRequest
    quotes: tuple[Quote, ...]
    intraday_bars: tuple[Bar, ...]
    daily_bars: tuple[Bar, ...]
    quality: DataQuality
    snapshot_id: str = field(init=False)
    content_hash: str = field(init=False)
    canonical_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be blank")
        if self.quality.provider != self.provider:
            raise ValueError("quality provider must match snapshot provider")
        quotes = _normalize_quotes(tuple(self.quotes))
        intraday_bars = _normalize_bars(tuple(self.intraday_bars), collection_name="intraday_bars")
        daily_bars = _normalize_bars(tuple(self.daily_bars), collection_name="daily_bars")
        requested_ids = {instrument.instrument_id for instrument in self.request.instruments}
        for collection_name, observations in (
            ("quotes", quotes),
            ("intraday_bars", intraday_bars),
            ("daily_bars", daily_bars),
        ):
            observed_ids = {observation.instrument_id for observation in observations}
            if not observed_ids <= requested_ids:
                raise ValueError("snapshot may only contain requested instruments")
            if observed_ids and observed_ids != requested_ids:
                raise ValueError(f"{collection_name} must cover every requested instrument")
        object.__setattr__(self, "quotes", quotes)
        object.__setattr__(self, "intraday_bars", intraday_bars)
        object.__setattr__(self, "daily_bars", daily_bars)
        canonical_json = _canonical_json(self.canonical_payload())
        content_hash = sha256(canonical_json.encode("utf-8")).hexdigest()
        object.__setattr__(self, "canonical_json", canonical_json)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "snapshot_id", f"market-data:v1:{content_hash}")

    @property
    def is_actionable(self) -> bool:
        """Gate downstream action on explicit quality evidence alone."""

        return (
            self.quality.is_actionable
            and bool(self.quotes)
            and bool(self.intraday_bars)
            and bool(self.daily_bars)
        )

    def canonical_payload(self) -> dict[str, object]:
        """Return complete request and result evidence used to build the content hash."""

        return {
            "daily_bars": [bar.canonical_payload() for bar in self.daily_bars],
            "intraday_bars": [bar.canonical_payload() for bar in self.intraday_bars],
            "provider": self.provider,
            "quality": self.quality.canonical_payload(),
            "quotes": [quote.canonical_payload() for quote in self.quotes],
            "request": self.request.canonical_payload(),
            "snapshot_format_version": "1",
        }


def _instrument_payload(instrument: Instrument) -> dict[str, str]:
    return {
        "created_at": canonical_datetime(instrument.created_at),
        "instrument_id": str(instrument.instrument_id),
        "market": instrument.market,
        "symbol": instrument.symbol,
    }


def _normalize_quotes(quotes: tuple[Quote, ...]) -> tuple[Quote, ...]:
    """Reject duplicate quote identities and sort equivalent evidence deterministically."""

    ordered = tuple(sorted(quotes, key=_quote_identity))
    _reject_duplicate_identities(
        tuple(_quote_identity(quote) for quote in ordered), collection_name="quote"
    )
    return ordered


def _normalize_bars(bars: tuple[Bar, ...], *, collection_name: str) -> tuple[Bar, ...]:
    """Reject duplicate bar identities and sort one snapshot collection deterministically."""

    ordered = tuple(sorted(bars, key=_bar_identity))
    _reject_duplicate_identities(
        tuple(_bar_identity(bar) for bar in ordered), collection_name=collection_name
    )
    return ordered


def _quote_identity(quote: Quote) -> tuple[str, str]:
    return (str(quote.instrument_id), canonical_datetime(quote.as_of))


def _bar_identity(bar: Bar) -> tuple[str, str, str]:
    return (
        str(bar.instrument_id),
        canonical_datetime(bar.starts_at),
        canonical_datetime(bar.ends_at),
    )


def _reject_duplicate_identities(
    identities: tuple[tuple[str, ...], ...], *, collection_name: str
) -> None:
    if any(
        current == previous for previous, current in zip(identities, identities[1:], strict=False)
    ):
        raise ValueError(f"duplicate {collection_name} identity")


def _normalized_labels(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{field_name} must contain non-blank strings")
    return tuple(sorted({value.strip().lower() for value in values}))
