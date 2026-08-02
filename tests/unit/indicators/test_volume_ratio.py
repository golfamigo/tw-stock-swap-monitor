"""Same-time volume-ratio indicator behavior."""

from datetime import date, timedelta
from decimal import Decimal

from app.domain.values import MarketSession
from app.indicators.volume_ratio import SameTimeVolumeRatioIndicator
from tests.unit.indicators.conftest import (
    INSTRUMENT_A,
    MARKET,
    FixtureCalendar,
    at,
    bar,
    snapshot,
)


class EarlyCloseCalendar(FixtureCalendar):
    """Use a shorter morning session only for the prior reference date."""

    def sessions_for(self, market: str, session_date: date) -> tuple[MarketSession, ...]:
        if session_date == date(2030, 1, 9):
            return (
                MarketSession(
                    opens_at=at(session_date, 9, 0),
                    closes_at=at(session_date, 9, 50),
                ),
            )
        return super().sessions_for(market, session_date)


def test_compares_current_volume_with_two_prior_same_time_sessions() -> None:
    calendar = FixtureCalendar()
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=calendar,
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (bar(date(2030, 1, 9), 9, 0, duration=timedelta(minutes=15), volume=Decimal("4")),),
            ),
            snapshot(
                date(2030, 1, 8),
                (bar(date(2030, 1, 8), 9, 0, duration=timedelta(minutes=15), volume=Decimal("6")),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 0, duration=timedelta(minutes=15), volume=Decimal("10")),),
        )
    )

    assert result.actionable
    assert result.value == Decimal("2")
    assert type(result.value) is Decimal
    assert result.evidence["lookback_days"] == "2"
    assert result.evidence["bar_width_microseconds"] == "900000000us"


def test_skips_holidays_when_finding_calendar_lookback_dates() -> None:
    holiday = date(2030, 1, 9)
    calendar = FixtureCalendar(holidays=frozenset({holiday}))
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=calendar,
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 8),
                (
                    bar(
                        date(2030, 1, 8), 13, 0, duration=timedelta(minutes=15), volume=Decimal("4")
                    ),
                ),
            ),
            snapshot(
                date(2030, 1, 7),
                (
                    bar(
                        date(2030, 1, 7), 13, 0, duration=timedelta(minutes=15), volume=Decimal("6")
                    ),
                ),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 13, 0, duration=timedelta(minutes=15), volume=Decimal("10")),),
        )
    )

    assert result.actionable
    assert result.value == Decimal("2")
    assert result.evidence["session_segment"] == "1"


def test_marks_metric_missing_when_lookback_history_is_insufficient() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (bar(date(2030, 1, 9), 9, 0, duration=timedelta(minutes=15), volume=Decimal("4")),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 0, duration=timedelta(minutes=15), volume=Decimal("10")),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "insufficient_history"
    assert "2030-01-08" in result.evidence["missing_session_dates"]


def test_marks_metric_missing_when_same_segment_reference_bar_is_absent() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (bar(date(2030, 1, 9), 9, 0, duration=timedelta(minutes=15), volume=Decimal("4")),),
            ),
            snapshot(
                date(2030, 1, 8),
                (
                    bar(
                        date(2030, 1, 8), 13, 0, duration=timedelta(minutes=15), volume=Decimal("6")
                    ),
                ),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 0, duration=timedelta(minutes=15), volume=Decimal("10")),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "missing_reference_bar"
    assert "2030-01-08" in result.evidence["missing_session_dates"]


def test_requires_the_same_bar_offset_within_a_session_segment() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (bar(date(2030, 1, 9), 9, 0, duration=timedelta(minutes=15)),),
            ),
            snapshot(
                date(2030, 1, 8),
                (bar(date(2030, 1, 8), 9, 15, duration=timedelta(minutes=15)),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 15, duration=timedelta(minutes=15)),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "missing_reference_bar"
    assert "2030-01-09" in result.evidence["missing_session_dates"]


def test_rejects_history_whose_reference_bar_date_mismatches_its_snapshot_date() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (bar(date(2030, 1, 8), 9, 0, duration=timedelta(minutes=15)),),
            ),
            snapshot(
                date(2030, 1, 8),
                (bar(date(2030, 1, 8), 9, 0, duration=timedelta(minutes=15)),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 0, duration=timedelta(minutes=15)),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "history_date_mismatch"
    assert result.evidence["mismatched_history_dates"] == "2030-01-09"


def test_invalid_current_source_bars_are_retained_as_unavailable_evidence() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (
                bar(current_date, 9, 0, duration=timedelta(minutes=2)),
                bar(current_date, 9, 1, duration=timedelta(minutes=2)),
            ),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "invalid_current_source_bars"
    assert "overlap" in result.evidence["validation_error"]


def test_out_of_session_current_bars_are_retained_as_unavailable_evidence() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
    )

    result = indicator.calculate(snapshot(current_date, (bar(current_date, 11, 0),)))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "invalid_current_source_bars"
    assert "outside a trading session" in result.evidence["validation_error"]


def test_invalid_history_source_bars_are_retained_as_unavailable_evidence() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (
                    bar(date(2030, 1, 9), 9, 0, duration=timedelta(minutes=2)),
                    bar(date(2030, 1, 9), 9, 1, duration=timedelta(minutes=2)),
                ),
            ),
            snapshot(
                date(2030, 1, 8),
                (bar(date(2030, 1, 8), 9, 0, duration=timedelta(minutes=15)),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 0, duration=timedelta(minutes=15)),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "invalid_history_source_bars"
    assert result.evidence["invalid_history_session_dates"] == "2030-01-09"
    assert "overlap" in result.evidence["validation_error"]


def test_rejects_same_offset_history_when_early_close_changes_resolved_interval() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=EarlyCloseCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                (bar(date(2030, 1, 9), 9, 45, duration=timedelta(minutes=5)),),
            ),
            snapshot(
                date(2030, 1, 8),
                (bar(date(2030, 1, 8), 9, 45, duration=timedelta(minutes=15)),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 45, duration=timedelta(minutes=15)),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "history_interval_mismatch"
    assert result.evidence["interval_mismatch_session_dates"] == "2030-01-09"


def test_quality_gate_prevents_an_actionable_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=15),
        lookback_days=2,
        history=(),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 0, duration=timedelta(minutes=15)),),
            bars_complete=False,
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "snapshot_not_actionable"
