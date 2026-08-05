"""Same-time volume-ratio indicator behavior."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
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


def test_lunch_break_current_bars_are_market_closed_evidence() -> None:
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
    assert result.reason == "current_request_market_closed"
    assert result.evidence["current_market_status"] == "no_effective_session"


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


@pytest.mark.parametrize(
    ("source_minutes", "expected_reason"),
    (
        ((1, 2), "missing_current_bar"),
        ((0, 1), "missing_current_bar"),
        ((0, 2), "incomplete_current_source_coverage"),
    ),
    ids=("leading-gap", "trailing-gap", "interior-gap"),
)
def test_gapped_current_window_cannot_produce_an_actionable_volume_ratio(
    source_minutes: tuple[int, int], expected_reason: str
) -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            tuple(bar(current_date, 9, minute) for minute in source_minutes),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    ("source_minutes", "expected_reason"),
    (
        ((1, 2), "missing_reference_bar"),
        ((0, 1), "missing_reference_bar"),
        ((0, 2), "incomplete_history_source_coverage"),
    ),
    ids=("leading-gap", "trailing-gap", "interior-gap"),
)
def test_gapped_historical_window_cannot_produce_an_actionable_volume_ratio(
    source_minutes: tuple[int, int], expected_reason: str
) -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                tuple(bar(date(2030, 1, 9), 9, minute) for minute in source_minutes),
            ),
            snapshot(
                date(2030, 1, 8),
                tuple(bar(date(2030, 1, 8), 9, minute) for minute in (0, 1, 2)),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            tuple(bar(current_date, 9, minute) for minute in (0, 1, 2)),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == expected_reason


def test_incomplete_final_current_bucket_cannot_fall_back_to_an_earlier_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    source_snapshot = snapshot(
        current_date,
        tuple(bar(current_date, 9, minute) for minute in range(5)),
    )
    requested_through_final_bucket = replace(
        source_snapshot.request,
        intraday_end=source_snapshot.request.intraday_start.replace(hour=9, minute=6),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                tuple(bar(date(2030, 1, 9), 9, minute) for minute in range(3)),
            ),
            snapshot(
                date(2030, 1, 8),
                tuple(bar(date(2030, 1, 8), 9, minute) for minute in range(3)),
            ),
        ),
    )

    result = indicator.calculate(replace(source_snapshot, request=requested_through_final_bucket))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "incomplete_current_source_coverage"
    assert result.evidence["required_end"].endswith("09:06:00+08:00")


def test_current_ratio_is_actionable_when_request_ends_on_complete_bucket() -> None:
    current_date = date(2030, 1, 10)
    source_snapshot = snapshot(
        current_date,
        tuple(bar(current_date, 9, minute) for minute in range(3)),
    )
    requested_through_complete_bucket = replace(
        source_snapshot.request,
        intraday_end=source_snapshot.request.intraday_start.replace(hour=9, minute=3),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                tuple(bar(date(2030, 1, 9), 9, minute) for minute in range(3)),
            ),
            snapshot(
                date(2030, 1, 8),
                tuple(bar(date(2030, 1, 8), 9, minute) for minute in range(3)),
            ),
        ),
    )

    result = indicator.calculate(
        replace(source_snapshot, request=requested_through_complete_bucket)
    )

    assert result.actionable
    assert result.value == Decimal("1")


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


def test_full_request_missing_its_final_session_cannot_use_morning_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    source_snapshot = snapshot(
        current_date,
        tuple(bar(current_date, 9 + minute // 60, minute % 60) for minute in range(120)),
    )
    full_day_request = replace(source_snapshot.request, intraday_end=at(current_date, 14, 0))
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=1),
        lookback_days=2,
        history=(
            snapshot(date(2030, 1, 9), (bar(date(2030, 1, 9), 10, 59),)),
            snapshot(date(2030, 1, 8), (bar(date(2030, 1, 8), 10, 59),)),
        ),
    )

    result = indicator.calculate(replace(source_snapshot, request=full_day_request))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "incomplete_current_source_coverage"
    assert result.evidence["session_segment"] == "1"
    assert result.evidence["required_end"].endswith("14:00:00+08:00")


def test_full_cross_break_request_uses_final_session_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    full_day_bars = (
        *(bar(current_date, 9 + minute // 60, minute % 60) for minute in range(120)),
        *(bar(current_date, 13, minute) for minute in range(60)),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=1),
        lookback_days=2,
        history=(
            snapshot(date(2030, 1, 9), (bar(date(2030, 1, 9), 13, 59),)),
            snapshot(date(2030, 1, 8), (bar(date(2030, 1, 8), 13, 59),)),
        ),
    )

    result = indicator.calculate(snapshot(current_date, full_day_bars))

    assert result.actionable
    assert result.value == Decimal("1")
    assert result.evidence["session_segment"] == "1"


def test_current_leading_gap_in_requested_range_blocks_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    source_snapshot = snapshot(
        current_date,
        tuple(bar(current_date, 9, minute) for minute in range(3, 6)),
    )
    requested_range = replace(
        source_snapshot.request,
        intraday_start=at(current_date, 9, 0),
        intraday_end=at(current_date, 9, 6),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                tuple(bar(date(2030, 1, 9), 9, minute) for minute in range(6)),
            ),
            snapshot(
                date(2030, 1, 8),
                tuple(bar(date(2030, 1, 8), 9, minute) for minute in range(6)),
            ),
        ),
    )

    result = indicator.calculate(replace(source_snapshot, request=requested_range))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "incomplete_current_source_coverage"
    assert result.evidence["coverage_gap"] == "leading"
    assert result.evidence["required_start"].endswith("09:00:00+08:00")


def test_historical_leading_gap_in_requested_range_blocks_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    incomplete_history = snapshot(
        date(2030, 1, 9),
        tuple(bar(date(2030, 1, 9), 9, minute) for minute in range(3, 6)),
    )
    incomplete_history_request = replace(
        incomplete_history.request,
        intraday_start=at(date(2030, 1, 9), 9, 0),
        intraday_end=at(date(2030, 1, 9), 9, 6),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            replace(incomplete_history, request=incomplete_history_request),
            snapshot(
                date(2030, 1, 8),
                tuple(bar(date(2030, 1, 8), 9, minute) for minute in range(6)),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            tuple(bar(current_date, 9, minute) for minute in range(6)),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "incomplete_history_source_coverage"
    assert result.evidence["incomplete_history_session_dates"] == "2030-01-09"
    assert result.evidence["history_coverage_gap"] == "leading"


def test_fully_covered_requested_ranges_produce_a_volume_ratio() -> None:
    current_date = date(2030, 1, 10)
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            snapshot(
                date(2030, 1, 9),
                tuple(bar(date(2030, 1, 9), 9, minute) for minute in range(6)),
            ),
            snapshot(
                date(2030, 1, 8),
                tuple(bar(date(2030, 1, 8), 9, minute) for minute in range(6)),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            tuple(bar(current_date, 9, minute) for minute in range(6)),
        )
    )

    assert result.actionable
    assert result.value == Decimal("1")


def test_lunch_only_history_cannot_use_out_of_request_morning_bars() -> None:
    current_date = date(2030, 1, 10)
    lunch_only_history = snapshot(
        date(2030, 1, 9),
        (bar(date(2030, 1, 9), 9, 3, duration=timedelta(minutes=3)),),
    )
    lunch_only_request = replace(
        lunch_only_history.request,
        intraday_start=at(date(2030, 1, 9), 11, 15),
        intraday_end=at(date(2030, 1, 9), 11, 45),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
        history=(
            replace(lunch_only_history, request=lunch_only_request),
            snapshot(
                date(2030, 1, 8),
                (bar(date(2030, 1, 8), 9, 3, duration=timedelta(minutes=3)),),
            ),
        ),
    )

    result = indicator.calculate(
        snapshot(
            current_date,
            (bar(current_date, 9, 3, duration=timedelta(minutes=3)),),
        )
    )

    assert not result.actionable
    assert result.value is None
    assert result.reason == "history_request_market_closed"
    assert result.evidence["history_market_status"] == "no_effective_session"
    assert result.evidence["ineligible_history_session_dates"] == "2030-01-09"


def test_lunch_only_current_request_is_explicitly_market_closed_evidence() -> None:
    current_date = date(2030, 1, 10)
    source_snapshot = snapshot(
        current_date,
        (bar(current_date, 9, 3, duration=timedelta(minutes=3)),),
    )
    lunch_only_request = replace(
        source_snapshot.request,
        intraday_start=at(current_date, 11, 15),
        intraday_end=at(current_date, 11, 45),
    )
    indicator = SameTimeVolumeRatioIndicator(
        calendar=FixtureCalendar(),
        market=MARKET,
        instrument_id=INSTRUMENT_A,
        bar_width=timedelta(minutes=3),
        lookback_days=2,
    )

    result = indicator.calculate(replace(source_snapshot, request=lunch_only_request))

    assert not result.actionable
    assert result.value is None
    assert result.reason == "current_request_market_closed"
    assert result.evidence["current_market_status"] == "no_effective_session"
