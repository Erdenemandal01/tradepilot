from __future__ import annotations

import math

import pandas as pd
import pytest

from backend.app.core import candles as cd
from backend.app.core import filters as fl
from backend.app.core.signal import BUY, HOLD, INSUFFICIENT_BARS, SELL, Levels, SignalResult, evaluate

M15 = 900
START = 1_794_000_000 - 1_794_000_000 % 3600  # an exact hour


def proposal(side: str | None, signal_time: int, *, entry: float = 4000.0, reason: str | None = None) -> SignalResult:
    actionable = side in (BUY, SELL)
    direction = 1 if side == BUY else -1
    levels = (
        Levels(entry, entry - 1, entry + 1, entry - direction * 6, entry + direction * 8, 6.0, 8.0)
        if actionable
        else None
    )
    return SignalResult(
        signal=side,
        score=None if side is None else {BUY: 4, SELL: -4, HOLD: 1}[side],
        strength=None,
        conditions=(),
        indicators={},
        levels=levels,
        candle_time=signal_time - M15,
        signal_time=signal_time,
        expires_at=signal_time + 2 * M15 if actionable else None,
        reason=reason,
    )


def trend(count: int, slope: float = 0.5, start: int = START) -> pd.DataFrame:
    """A wavy trend: the waves keep RSI off its extremes so the rule can score."""
    rows = []
    previous = 4000.0
    for i in range(count):
        close = 4000.0 + slope * i + 3.0 * math.sin(i / 3.0)
        rows.append(
            {
                "time": start + i * M15,
                "open": previous,
                "high": max(previous, close) + 0.4,
                "low": min(previous, close) - 0.4,
                "close": close,
                "volume": 1.0,
                "closed": True,
            }
        )
        previous = close
    return cd.validate(pd.DataFrame(rows), "M15")


# --- decide ---------------------------------------------------------------


def test_a_proposal_that_passes_every_filter_is_sent():
    decision = fl.decide(proposal(BUY, START), h1_score=2, previous=None)
    assert decision.sent and decision.blocked is None and decision.run == 1


def test_hold_is_neither_sent_nor_blocked():
    decision = fl.decide(proposal(HOLD, START), h1_score=5, previous=None)
    assert not decision.sent and decision.blocked is None and decision.run == 0


def test_an_evaluation_without_enough_candles_keeps_its_reason():
    decision = fl.decide(proposal(None, START, reason=INSUFFICIENT_BARS), h1_score=None, previous=None)
    assert not decision.sent and decision.blocked == INSUFFICIENT_BARS


@pytest.mark.parametrize(
    ("side", "h1_score", "blocked"),
    [
        (BUY, 1, None),
        (BUY, 0, fl.HTF_CONFLICT),
        (BUY, -3, fl.HTF_CONFLICT),
        (SELL, -1, None),
        (SELL, 0, fl.HTF_CONFLICT),
        (SELL, 2, fl.HTF_CONFLICT),
    ],
)
def test_h1_filter_blocks_at_the_plan_thresholds(side, h1_score, blocked):
    assert fl.decide(proposal(side, START), h1_score=h1_score, previous=None).blocked == blocked


def test_an_unknown_h1_score_blocks_instead_of_passing():
    assert fl.decide(proposal(BUY, START), h1_score=None, previous=None).blocked == INSUFFICIENT_BARS


def test_closed_market_and_stale_feed_block_before_anything_else():
    assert fl.decide(proposal(BUY, START), h1_score=3, previous=None, market_closed=True).blocked == fl.MARKET_CLOSED
    stale = fl.decide(proposal(BUY, START), h1_score=3, previous=None, market_closed=True, stale=True)
    assert stale.blocked == fl.STALE_FEED


def test_same_side_signal_is_a_duplicate_only_while_the_earlier_one_is_valid():
    first = fl.decide(proposal(BUY, START), h1_score=2, previous=None)
    during = fl.decide(proposal(BUY, START + M15), h1_score=2, previous=first)
    at_expiry = fl.decide(proposal(BUY, START + 2 * M15), h1_score=2, previous=first)
    opposite = fl.decide(proposal(SELL, START + M15), h1_score=-2, previous=first)
    assert during.blocked == fl.DUPLICATE and not during.sent
    assert at_expiry.sent and at_expiry.run == 2
    assert opposite.sent and opposite.run == 1


def test_a_run_restarts_after_a_pause_or_a_change_of_side():
    first = fl.decide(proposal(BUY, START), h1_score=2, previous=None)
    second = fl.decide(proposal(BUY, START + 2 * M15), h1_score=2, previous=first)
    third = fl.decide(proposal(BUY, START + 4 * M15), h1_score=2, previous=second)
    later = fl.decide(proposal(BUY, START + 40 * M15), h1_score=2, previous=third)
    assert (second.run, third.run, later.run) == (2, 3, 1)


# --- signal state and outcome ----------------------------------------------


def test_signal_state_follows_the_clock_and_the_entry_zone():
    decision = fl.decide(proposal(BUY, START, entry=4000.0), h1_score=2, previous=None)
    assert fl.signal_state(decision, 4000.5, START + 60) == "valid"
    assert fl.signal_state(decision, 4001.0, START + 60) == "valid"  # the zone's edge counts as inside
    assert fl.signal_state(decision, 4001.01, START + 60) == "outside_zone"
    assert fl.signal_state(decision, 3998.9, START + 60) == "outside_zone"
    assert fl.signal_state(decision, 4000.0, START + 2 * M15) == "expired"
    assert fl.signal_state(decision, None, START + 60) == "valid"  # no price: the zone cannot be judged


def later_candles(rows: list[tuple[float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"time": START + i * M15, "high": high, "low": low, "closed": True} for i, (high, low) in enumerate(rows)]
    )


def test_outcome_is_the_first_level_reached():
    buy = proposal(BUY, START, entry=4000.0)  # SL 3994, TP 4008
    assert fl.outcome(buy, later_candles([(4003, 3998), (4008.5, 4001)])) == ("tp", START + M15)
    assert fl.outcome(buy, later_candles([(4003, 3993.9), (4009, 4001)])) == ("sl", START)
    sell = proposal(SELL, START, entry=4000.0)  # SL 4006, TP 3992
    assert fl.outcome(sell, later_candles([(4003, 3991)])) == ("tp", START)
    assert fl.outcome(sell, later_candles([(4006, 3999)])) == ("sl", START)


def test_outcome_counts_the_stop_first_when_one_candle_touches_both():
    buy = proposal(BUY, START, entry=4000.0)
    assert fl.outcome(buy, later_candles([(4010, 3990)])) == ("sl", START)


def test_outcome_ignores_earlier_and_forming_candles():
    buy = proposal(BUY, START + M15, entry=4000.0)
    frame = later_candles([(4010, 3990), (4003, 3998), (4009, 4001)])
    frame.loc[2, "closed"] = False
    assert fl.outcome(buy, frame) == (None, None)
    assert fl.outcome(buy, later_candles([])) == (None, None)


# --- replay ----------------------------------------------------------------


def test_replay_matches_a_fresh_evaluation_of_every_prefix():
    frame = trend(1400)
    decisions = fl.replay(frame, "M15", count=6)
    assert len(decisions) == 6
    for offset, decision in zip(range(5, -1, -1), decisions):
        prefix = frame.iloc[: len(frame) - offset]
        assert decision.result == evaluate(prefix)


def test_replay_sends_signals_in_a_trend_and_never_two_valid_at_once():
    decisions = fl.replay(trend(1400), "M15", count=80)
    sent = [d for d in decisions if d.sent]
    assert sent, "the test trend should produce signals"
    assert {d.result.signal for d in sent} == {BUY}
    assert all(d.h1_score is not None and d.h1_score >= 1 for d in sent)
    for earlier, later in zip(sent, sent[1:]):
        assert later.result.signal_time >= earlier.result.expires_at
    assert any(d.blocked == fl.DUPLICATE for d in decisions)


def test_replay_uses_only_h1_candles_that_had_closed_by_then():
    frame = trend(1400)
    full = fl.replay(frame, "M15", count=3)
    # Later candles must not change an earlier decision: no look-ahead.
    shorter = fl.replay(frame.iloc[:-2], "M15", count=1)
    assert shorter[0] == full[0]


def test_replay_blocks_everything_while_the_market_is_closed():
    decisions = fl.replay(trend(1400), "M15", count=40, closed_hours=lambda t: True)
    assert not any(d.sent for d in decisions)
    assert any(d.blocked == fl.MARKET_CLOSED for d in decisions)


def test_replay_continues_from_the_previous_sent_signal():
    frame = trend(1400)
    history = fl.replay(frame.iloc[:-1], "M15", count=80)
    previous = next(d for d in reversed(history) if d.sent)
    step = fl.replay(frame, "M15", count=1, previous=previous)[0]
    whole = fl.replay(frame, "M15", count=81)[-1]
    assert step == whole


def test_replay_with_too_few_candles_reports_it():
    decisions = fl.replay(trend(120), "M15", count=2)
    assert [d.blocked for d in decisions] == [INSUFFICIENT_BARS, INSUFFICIENT_BARS]


def test_replay_skips_forming_candles_and_handles_small_frames():
    frame = trend(1400)
    frame.loc[frame.index[-1], "closed"] = False
    decisions = fl.replay(frame, "M15", count=1)
    assert decisions[0].result.candle_time == int(frame["time"].iloc[-2])
    assert fl.replay(frame.iloc[:0], "M15", count=5) == []


# --- candles.merge -----------------------------------------------------------


def forming_last(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.loc[frame.index[-1], "closed"] = False
    return frame


def test_merge_lays_the_fresh_tail_over_the_stored_candles():
    whole = trend(12)
    stored = forming_last(whole.iloc[:10])
    fresh = forming_last(whole.iloc[8:12])
    merged = cd.merge(stored, fresh, "M15")
    assert merged["time"].tolist() == whole["time"].tolist()
    assert merged["closed"].tolist() == [True] * 11 + [False]
    assert cd.merge(stored, fresh, "M15", keep=5)["time"].tolist() == whole["time"].tolist()[-5:]


def test_merge_refuses_a_tail_that_does_not_connect():
    whole = trend(20)
    stored = forming_last(whole.iloc[:10])
    assert cd.merge(stored, forming_last(whole.iloc[12:15]), "M15") is None  # a gap
    # The stored forming candle would stay unfinished in the middle.
    assert cd.merge(stored, forming_last(whole.iloc[10:13]), "M15") is None
    assert cd.merge(stored, forming_last(whole.iloc[3:6]), "M15") is None  # the source went backwards


def test_merge_with_nothing_stored_or_nothing_fresh():
    whole = forming_last(trend(6))
    assert cd.merge(whole.iloc[:0], whole, "M15", keep=4)["time"].tolist() == whole["time"].tolist()[-4:]
    assert cd.merge(whole, whole.iloc[:0], "M15").equals(whole)


def test_merge_bears_a_hole_the_stored_history_already_has():
    whole = trend(60)
    holed = whole.drop(whole.index[20]).reset_index(drop=True)  # one candle the source never had
    stored = forming_last(holed.iloc[:-2])
    merged = cd.merge(stored, forming_last(holed.iloc[-4:]), "M15")
    assert merged is not None and merged["time"].tolist() == holed["time"].tolist()
    assert [gap.missing for gap in cd.find_gaps(merged, "M15")] == [1]  # the old hole is kept, not healed by guessing


def test_merge_refuses_a_hole_that_was_not_there_before_and_bears_it_once_it_is_stored():
    whole = trend(20)
    stored = forming_last(whole.iloc[:10])
    tail = forming_last(whole.iloc[[8, 10, 11]])  # the candle after the stored closed ones is missing
    assert cd.merge(stored, tail, "M15") is None  # news: load the history again
    reloaded = forming_last(pd.concat([whole.iloc[:9], whole.iloc[10:12]], ignore_index=True))
    again = cd.merge(reloaded, tail, "M15")  # the source still has the hole: nothing more to gain by reloading
    assert again is not None and again["time"].tolist() == reloaded["time"].tolist()


def test_replay_stops_a_candle_that_closed_under_a_stale_price_and_does_not_count_it_as_sent():
    frame = trend(1400)
    plain = fl.replay(frame, "M15", count=80)
    at = next(i for i, d in enumerate(plain[:-1]) if d.sent and plain[i + 1].blocked == fl.DUPLICATE)
    stale_at = plain[at].result.signal_time

    held = fl.replay(frame, "M15", count=80, stale_times={stale_at})
    assert held[:at] == plain[:at]  # nothing before it changes
    assert held[at].blocked == fl.STALE_FEED and not held[at].sent and held[at].result == plain[at].result
    # The next proposal of that side is no duplicate of a signal that was never sent.
    assert held[at + 1].sent and held[at + 1].run == 1
    assert fl.replay(frame, "M15", count=80, stale_times=set()) == plain
