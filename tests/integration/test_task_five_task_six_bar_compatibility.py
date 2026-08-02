"""Offline contract between the deterministic source and calendar-aligned aggregation."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from app.calendar.mock import MockTradingCalendar
from app.data_sources.models import MarketDataRequest
from app.data_sources.providers.mock import MockMarketDataProvider
from app.domain.entities import Instrument
from app.domain.values import MarketSession
from app.indicators.bars import aggregate_bars

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


def _request(interval: timedelta) -> MarketDataRequest:
    return MarketDataRequest(
        request_id=UUID("00000000-0000-0000-0000-000000000701"),
        instruments=(
            Instrument(
                instrument_id=UUID("00000000-0000-0000-0000-000000000702"),
                market=MARKET,
                symbol="instrument-a",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            ),
        ),
        requested_at=datetime(2030, 1, 2, 14, 0, tzinfo=UTC),
        intraday_start=datetime(2030, 1, 2, 10, 0, tzinfo=UTC),
        intraday_end=datetime(2030, 1, 2, 13, 0, tzinfo=UTC),
        intraday_interval=interval,
        daily_start=TRADING_DAY,
        daily_end=TRADING_DAY + timedelta(days=1),
        maximum_data_delay=timedelta(minutes=5),
    )


@pytest.mark.parametrize("width", (timedelta(minutes=3), timedelta(minutes=15)))
def test_mock_intraday_grid_is_directly_compatible_with_task_six_aggregation(
    width: timedelta,
) -> None:
    calendar = _calendar()
    provider = MockMarketDataProvider(provider_name="mock-source-a", seed=17, calendar=calendar)
    snapshot = provider.get_intraday_bars(_request(width))

    aggregated = aggregate_bars(
        snapshot.intraday_bars,
        calendar=calendar,
        market=MARKET,
        width=width,
    )

    assert aggregated == snapshot.intraday_bars
    assert all(
        not (bar.starts_at < datetime(2030, 1, 2, 12, 0, tzinfo=UTC) < bar.ends_at)
        for bar in aggregated
    )
