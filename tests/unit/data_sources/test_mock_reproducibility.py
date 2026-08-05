"""Deterministic mock market-data regression tests."""

import socket
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from app.calendar.mock import MockTradingCalendar
from app.data_sources.models import DataQuality, MarketDataRequest
from app.data_sources.providers.mock import MockMarketDataProvider
from app.domain.entities import Instrument
from app.domain.values import MarketSession

MARKET = "source-a"
TRADING_DAY = date(2030, 1, 2)


def _calendar() -> MockTradingCalendar:
    return MockTradingCalendar(
        market=MARKET,
        timezone_name="Etc/UTC",
        trading_day=TRADING_DAY,
        sessions=(
            MarketSession(
                opens_at=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
                closes_at=datetime(2030, 1, 2, 11, 0, tzinfo=UTC),
            ),
            MarketSession(
                opens_at=datetime(2030, 1, 2, 12, 0, tzinfo=UTC),
                closes_at=datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
            ),
        ),
    )


def _request() -> MarketDataRequest:
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000101"),
        instruments=(
            Instrument(
                instrument_id=UUID("00000000-0000-0000-0000-000000000201"),
                market=MARKET,
                symbol="instrument-a",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            ),
        ),
        requested_at=datetime(2030, 1, 2, 14, 0, tzinfo=UTC),
        intraday_start=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
        intraday_interval=timedelta(minutes=15),
        daily_start=TRADING_DAY,
        daily_end=TRADING_DAY + timedelta(days=1),
        maximum_data_delay=timedelta(minutes=5),
    )


def _provider(
    *, seed: int, source_delay: timedelta = timedelta(minutes=1)
) -> MockMarketDataProvider:
    return MockMarketDataProvider(
        provider_name="mock-source-a",
        seed=seed,
        calendar=_calendar(),
        source_delay=source_delay,
    )


def test_same_seed_and_complete_request_are_byte_for_byte_reproducible() -> None:
    request = _request()

    first = _provider(seed=17).get_market_snapshot(request)
    second = _provider(seed=17).get_market_snapshot(request)

    assert first.canonical_json.encode("utf-8") == second.canonical_json.encode("utf-8")
    assert first.snapshot_id == second.snapshot_id
    assert first.content_hash == second.content_hash
    assert first.intraday_bars == second.intraday_bars


def test_different_seed_changes_the_generated_snapshot() -> None:
    request = _request()

    first = _provider(seed=17).get_market_snapshot(request)
    second = _provider(seed=18).get_market_snapshot(request)

    assert first.intraday_bars != second.intraday_bars
    assert first.snapshot_id != second.snapshot_id


def test_equal_instants_in_different_aware_timezones_produce_identical_snapshots() -> None:
    request = replace(
        _request(),
        intraday_start=datetime(2030, 1, 2, 10, 15, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 2, 10, 45, tzinfo=UTC),
    )
    alternate_timezone = ZoneInfo("Etc/GMT-8")
    equivalent_request = replace(
        request,
        requested_at=request.requested_at.astimezone(alternate_timezone),
        intraday_start=request.intraday_start.astimezone(alternate_timezone),
        intraday_end=request.intraday_end.astimezone(alternate_timezone),
    )

    first = _provider(seed=17).get_market_snapshot(request)
    second = _provider(seed=17).get_market_snapshot(equivalent_request)

    assert first.intraday_bars == second.intraday_bars
    assert first.snapshot_id == second.snapshot_id


def test_mock_provider_rejects_intraday_request_edges_outside_the_session_grid() -> None:
    request = replace(
        _request(),
        intraday_start=datetime(2030, 1, 2, 10, 5, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 2, 10, 35, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="session grid"):
        _provider(seed=17).get_intraday_bars(request)


def test_no_session_intraday_request_marks_snapshot_quality_as_market_closed() -> None:
    closed_date = date(2030, 1, 3)
    request = replace(
        _request(),
        intraday_start=datetime(2030, 1, 3, 10, 0, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 3, 11, 0, tzinfo=UTC),
        daily_start=closed_date,
        daily_end=closed_date + timedelta(days=1),
    )

    market_snapshot = _provider(seed=17).get_intraday_bars(request)

    assert market_snapshot.intraday_bars == ()
    assert market_snapshot.quality.anomalies == ("market_closed",)
    assert not market_snapshot.quality.is_actionable


def test_lunch_break_intraday_request_marks_all_snapshot_paths_as_market_closed() -> None:
    request = replace(
        _request(),
        intraday_start=datetime(2030, 1, 2, 11, 15, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 2, 11, 45, tzinfo=UTC),
    )
    provider = _provider(seed=17)

    intraday_snapshot = provider.get_intraday_bars(request)
    complete_snapshot = provider.get_market_snapshot(request)

    assert intraday_snapshot.intraday_bars == ()
    assert intraday_snapshot.quality.anomalies == ("market_closed",)
    assert not intraday_snapshot.is_actionable
    assert complete_snapshot.intraday_bars == ()
    assert complete_snapshot.quality.anomalies == ("market_closed",)
    assert not complete_snapshot.is_actionable


def test_no_session_daily_request_marks_snapshot_quality_as_market_closed() -> None:
    closed_date = date(2030, 1, 3)
    request = replace(
        _request(),
        daily_start=closed_date,
        daily_end=closed_date + timedelta(days=1),
    )

    market_snapshot = _provider(seed=17).get_daily_bars(request)

    assert market_snapshot.daily_bars == ()
    assert market_snapshot.quality.anomalies == ("market_closed",)
    assert not market_snapshot.quality.is_actionable


def test_data_quality_uses_elapsed_utc_time_across_a_dst_fall_back() -> None:
    timezone = ZoneInfo("America/New_York")
    source_timestamp = datetime(2024, 11, 3, 1, 30, tzinfo=timezone, fold=0)
    fetched_at = datetime(2024, 11, 3, 2, 30, tzinfo=timezone)

    quality = DataQuality(
        provider="mock-source-a",
        fetched_at=fetched_at,
        source_timestamp=source_timestamp,
        delay=timedelta(hours=2),
        stale=False,
        missing_fields=(),
        bars_complete=True,
        anomalies=(),
        confidence=Decimal("1"),
    )

    assert quality.delay == timedelta(hours=2)
    with pytest.raises(ValueError, match="delay"):
        replace(quality, delay=timedelta(hours=1))


def test_mock_source_delay_preserves_elapsed_time_across_a_dst_fall_back() -> None:
    timezone = ZoneInfo("America/New_York")
    fetched_at = datetime(2024, 11, 3, 2, 30, tzinfo=timezone)
    request = replace(
        _request(),
        requested_at=fetched_at,
        maximum_data_delay=timedelta(hours=3),
    )

    snapshot = _provider(seed=17, source_delay=timedelta(hours=2)).get_quotes(request)

    assert snapshot.quality.delay == timedelta(hours=2)
    assert snapshot.quality.fetched_at.astimezone(
        UTC
    ) - snapshot.quality.source_timestamp.astimezone(UTC) == timedelta(hours=2)
    assert not snapshot.quality.stale


def test_snapshot_rejects_partial_evidence_for_requested_instruments() -> None:
    first_request = _request()
    request = replace(
        first_request,
        instruments=(
            *first_request.instruments,
            Instrument(
                instrument_id=UUID("00000000-0000-0000-0000-000000000202"),
                market=MARKET,
                symbol="instrument-b",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            ),
        ),
    )
    complete_snapshot = _provider(seed=17).get_market_snapshot(request)

    with pytest.raises(ValueError, match="quotes must cover"):
        replace(complete_snapshot, quotes=complete_snapshot.quotes[:1])
    with pytest.raises(ValueError, match="intraday_bars must cover"):
        replace(complete_snapshot, intraday_bars=complete_snapshot.intraday_bars[:1])


def test_case_and_whitespace_variants_of_required_ohlcv_labels_block_action() -> None:
    quality = DataQuality(
        provider="mock-source-a",
        fetched_at=datetime(2030, 1, 2, 14, 0, tzinfo=UTC),
        source_timestamp=datetime(2030, 1, 2, 13, 59, tzinfo=UTC),
        delay=timedelta(minutes=1),
        stale=False,
        missing_fields=(" OPEN ",),
        bars_complete=True,
        anomalies=(),
        confidence=Decimal("1"),
    )

    assert quality.missing_fields == ("open",)
    assert not quality.is_actionable


def test_stale_or_incomplete_quality_cannot_be_actionable() -> None:
    stale_snapshot = _provider(seed=17, source_delay=timedelta(minutes=6)).get_market_snapshot(
        _request()
    )
    incomplete_quality = DataQuality(
        provider="mock-source-a",
        fetched_at=datetime(2030, 1, 2, 14, 0, tzinfo=UTC),
        source_timestamp=datetime(2030, 1, 2, 13, 59, tzinfo=UTC),
        delay=timedelta(minutes=1),
        stale=False,
        missing_fields=("volume",),
        bars_complete=False,
        anomalies=(),
        confidence=Decimal("1"),
    )

    assert stale_snapshot.quality.stale
    assert not stale_snapshot.is_actionable
    assert not incomplete_quality.is_actionable


def test_mock_provider_does_not_open_network_connections(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("mock market-data provider must remain offline")

    monkeypatch.setattr(socket, "create_connection", reject_network)

    snapshot = _provider(seed=17).get_market_snapshot(_request())

    assert snapshot.is_actionable


@pytest.mark.parametrize("seed", (0, -1, -17))
def test_zero_and_negative_seeds_produce_only_positive_ohlc_prices(seed: int) -> None:
    source_request = _request()
    varied_instruments = (
        *source_request.instruments,
        Instrument(
            instrument_id=UUID(int=246541),
            market=MARKET,
            symbol="instrument-246541",
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
        ),
        Instrument(
            instrument_id=UUID("00000000-0000-0000-0000-000000000901"),
            market=MARKET,
            symbol="instrument-901",
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
        ),
    )

    snapshot = _provider(seed=seed).get_market_snapshot(
        replace(source_request, instruments=varied_instruments)
    )

    generated_bars = (*snapshot.intraday_bars, *snapshot.daily_bars)
    assert generated_bars
    assert all(
        bar.open > Decimal("0")
        and bar.high > Decimal("0")
        and bar.low > Decimal("0")
        and bar.close > Decimal("0")
        for bar in generated_bars
    )


def test_large_neighboring_seeds_produce_distinct_quote_snapshot_evidence() -> None:
    source_request = _request()
    lower_seed_snapshot = _provider(seed=10**40).get_quotes(source_request)
    higher_seed_snapshot = _provider(seed=10**40 + 1).get_quotes(source_request)

    assert lower_seed_snapshot.quotes[0].price != higher_seed_snapshot.quotes[0].price
    assert lower_seed_snapshot.canonical_json != higher_seed_snapshot.canonical_json
    assert lower_seed_snapshot.content_hash != higher_seed_snapshot.content_hash
    assert lower_seed_snapshot.snapshot_id != higher_seed_snapshot.snapshot_id


def test_extremely_large_seed_produces_finite_deterministic_quote_evidence() -> None:
    huge_seed = 10**999_996
    source_request = _request()

    first = _provider(seed=huge_seed).get_quotes(source_request)
    second = _provider(seed=huge_seed).get_quotes(source_request)

    assert first.quotes[0].price.is_finite()
    assert first.quotes[0].price > Decimal("0")
    assert first.quotes == second.quotes
    assert first.canonical_json == second.canonical_json
    assert first.content_hash == second.content_hash
    assert first.snapshot_id == second.snapshot_id
