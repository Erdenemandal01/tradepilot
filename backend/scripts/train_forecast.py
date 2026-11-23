"""Fit the forecast model on the source's whole M15 history and measure it honestly.

    .venv/Scripts/python -m backend.scripts.train_forecast

What it does:

1. Downloads every M15 candle the price source has (public market data, read
   in pages with a pause between them). Closed candles only.
2. Walk-forward evaluation: fit on the past, forecast the next ten days,
   move on. Every forecast that is scored was made by a model that had never
   seen that stretch. This gives the record that is shown with the forecast.
   The shown record counts only forecasts made while the broker's market is
   probably open, by fits at least half the size of the final one. The
   closed hours and the full run are printed and stored beside it.
3. Fits the final model on everything and writes it, with that record, to
   data/forecast_model.json. The web app reads the file at start-up, so
   restart the app after training. It also counts how often the final model
   itself says "up" or "down" and prints it: the record is that of other
   fits, each with its own ridge strength, and cannot say that.

The printed result says plainly whether the model beat "the price stays where
it is". When it did not, the file is still written and the page says so next
to the forecast. If the source refuses the download, nothing is written and
an older model file stays as it was.

This script reads prices and writes one file. It sends no orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from backend.app.config import get_settings
from backend.app.core import candles as cd
from backend.app.core import forecast as fc
from backend.app.datasource import SOURCES, DataSource, DataSourceError, make_source
from backend.app.forecast import FILE_FORMAT, check_file

REFUSED = (418, 429)
WINDOW_CHECKS = 200  # forecasts made again from the live feed's short window, to compare


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def window_check(model: dict[str, Any], closed: pd.DataFrame, window: int, checks: int = WINDOW_CHECKS) -> dict[str, Any] | None:
    """How much a forecast changes when it is made from the live feed's window only.

    The evaluation computes the features on the whole history. The live feed
    keeps ``window`` candles, so its indicators start later. Here the same
    moments are forecast both ways and the difference at the fourth close is
    measured, in price units.
    """
    count = len(closed)
    if count <= window + 1:
        return None
    ends = np.unique(np.linspace(window + 1, count, num=checks, dtype=int))
    differences, flips, made = [], 0, 0
    for end in ends:
        whole = fc.predict(model, closed.iloc[:end])
        short = fc.predict(model, closed.iloc[end - window : end])
        if whole is None or short is None:
            continue
        made += 1
        differences.append(abs(whole["points"][-1]["close"] - short["points"][-1]["close"]))
        flips += whole["direction"] != short["direction"]
    if not made:
        return None
    return {
        "window": int(window),
        "checked": int(made),
        "mean_abs_difference": float(np.mean(differences)),
        "max_abs_difference": float(np.max(differences)),
        "direction_changed": int(flips),
    }


def side_line(record: dict[str, Any] | None) -> str:
    """One line for a record that is kept beside the shown one."""
    if record is None:
        return "no forecasts"
    hit = f"{record['hit_rate']:.1%} right of {record['calls']} calls" if record["calls"] else "no calls"
    return (
        f"{record['samples']} forecasts, error {record['mae']:.3f} / {record['baseline_mae']:.3f}, {hit}, "
        f"band {record['coverage']:.1%}"
    )


def describe(evaluation: fc.Evaluation, out: Callable[[str], None]) -> None:
    skill = evaluation.skill
    out("")
    out("Walk-forward result (every forecast scored here was made by a model that had not seen that stretch):")
    out(
        f"  what is scored    forecasts made while the broker's market is probably open, by the "
        f"{evaluation.scored_blocks} of {evaluation.blocks} fits"
    )
    out(
        f"                    that saw at least {fc.COMPARABLE_TRAIN:.0%} of the final model's rows "
        f"(the smallest of them: {skill['train_rows']} rows)"
    )
    out(f"  period            {utc(skill['from'])} to {utc(skill['to'])}")
    out(f"  forecasts scored  {skill['samples']}")
    out("  error of the expected close, in price units (model / price stays where it is):")
    for step, (ours, base) in enumerate(zip(evaluation.mae_by_step, evaluation.baseline_mae_by_step), start=1):
        out(f"    {step * 15:>2} min ahead   {ours:.3f} / {base:.3f}")
    gain = skill["baseline_mae"] - skill["mae"]
    out(f"  at 60 minutes the model's error is {abs(gain):.3f} {'lower' if gain > 0 else 'HIGHER'} than the baseline's")
    out(f"    lower 95% bound of that gain: {evaluation.mae_gain_floor:+.3f} (must be above 0)")
    if skill["calls"]:
        out(
            f"  direction calls   {skill['calls']} of {skill['samples']} forecasts: {skill['calls_up']} up, "
            f"{skill['calls_down']} down (the rest were too close to a coin toss, or the line said otherwise)"
        )
        out(f"    right at the 4th close: {skill['hit_rate']:.1%}; the price really was higher {evaluation.up_share:.1%} of the time")
        out(
            f"    'always {skill['hit_rate_baseline_call']}' on the same calls would have been right "
            f"{skill['hit_rate_baseline']:.1%}"
        )
        out(f"    lower 95% bound of the hit rate: {evaluation.hit_floor:.1%} (must be above 50%)")
    else:
        # Said in words: a bare "0 calls, 0% right" would read like a measured hit rate.
        out(f"  direction calls   NONE. Not one of the {skill['samples']} scored forecasts said \"up\" or \"down\":")
        out("    every one was too close to a coin toss, or its line said otherwise.")
        out("    So there is no hit rate. Nothing was measured about the model's direction word.")
    out(f"  band              {skill['coverage']:.1%} of the 4th closes fell inside it (it is built to hold 80%)")
    out("  kept beside it, not part of the shown record (error model / baseline):")
    out(f"    the same fits, market probably closed: {side_line(evaluation.closed_market)}")
    out(f"    all {evaluation.blocks} fits, all hours:             {side_line(evaluation.all_forecasts)}")
    out("")
    if skill["better_than_baseline"]:
        out("VERDICT: on this history the model was measurably better than 'the price stays where it is'.")
        out("That is a record of the past, not a promise. It is a forecast, not a signal.")
    else:
        out("VERDICT: the model is NOT better than 'the price stays where it is'.")
        reasons = []
        if not skill["calls"]:
            reasons.append(f"it made no direction call at all (at least {fc.MIN_CALLS} are needed)")
        elif skill["calls"] < fc.MIN_CALLS:
            reasons.append(f"it made only {skill['calls']} direction calls (at least {fc.MIN_CALLS} are needed)")
        if not skill["mae"] < skill["baseline_mae"]:
            reasons.append("its error is not lower than the baseline's")
        elif not evaluation.mae_gain_floor > 0:
            reasons.append("its error is lower by too little to tell from chance")
        if skill["calls"] and not evaluation.hit_floor > 0.5:
            reasons.append("its direction calls are not clearly better than a coin toss")
        for reason in reasons:
            out(f"  - {reason}")
        out("The forecast line is still written, and the page will say next to it that it has no measured edge.")
        out("Do not act on it.")


def own_calls_lines(own: dict[str, int], skill: dict[str, Any]) -> list[str]:
    """What is said about the final model's own direction calls (``core.forecast.own_calls``).

    The walk-forward record is that of other fits, and each fit chooses its
    own ridge strength. So the record cannot say how often the final model
    leaves the flat zone. That is counted on the final model and said here,
    and when the record holds no call at all, so is what follows from it.
    """
    calls = own["up"] + own["down"]
    if not calls:
        return [
            f"The final model itself makes no direction call at all on the {own['rows']} candles it was fitted on: "
            'there its word is always "flat".'
        ]
    lines = [
        f'The final model itself says "up" or "down" on {calls} of the {own["rows"]} candles it was fitted on '
        f"({own['up']} up, {own['down']} down; {own['open_market_calls']} of them in open-market hours).",
        "  That is a count, not a record: the model has seen these candles.",
    ]
    if not skill["calls"]:
        lines.append("  The fits scored above never did: each fit chooses its own ridge strength.")
        lines.append('  So when the page shows "up" or "down", that word has no measured record at all.')
    return lines


def write_model(path: Path, stored: dict[str, Any]) -> None:
    """Write the file whole or not at all: a half-written model must never be read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(stored, allow_nan=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def train(
    source: DataSource,
    out_path: Path,
    *,
    window: int,
    clock: Callable[[], float] = time.time,
    out: Callable[[str], None] = print,
) -> int:
    """Download, measure, fit, write. Returns the process exit code."""
    out(f"Reading the M15 history of {source.symbol} from the {source.name} source ...")
    try:
        frame = source.fetch_history(fc.TIMEFRAME)
    except NotImplementedError as error:
        out(f"STOPPED: {error}. No model was written.")
        return 1
    except DataSourceError as error:
        if error.status in REFUSED:
            wait = f" It asked to wait {int(error.retry_after)} s." if error.retry_after else ""
            out(f"STOPPED: the source refused the download (HTTP {error.status}).{wait} No model was written.")
        else:
            out(f"STOPPED: the source gave no history: {error}. No model was written.")
        return 1
    closed = frame[frame["closed"]].reset_index(drop=True)
    if closed.empty:
        out("STOPPED: the source has no closed candles. No model was written.")
        return 1
    first, last = int(closed["time"].iloc[0]), int(closed["time"].iloc[-1])
    gaps = cd.find_gaps(closed, fc.TIMEFRAME)
    missing = sum(gap.missing for gap in gaps)
    out(f"  {len(closed)} closed candles, {utc(first)} to {utc(last + fc.STEP)}")
    out(f"  holes in the source's own history: {len(gaps)} ({missing} candles); nothing is filled in")

    evaluation = fc.walk_forward(closed)
    if evaluation is None:
        out(
            f"STOPPED: too little history to measure the model (it needs about "
            f"{fc.MIN_TRAIN + 1300} candles before the first forecast can be scored). No model was written."
        )
        return 1
    describe(evaluation, out)

    model = fc.fit(closed)
    out("")
    for line in own_calls_lines(fc.own_calls(model, closed), evaluation.skill):
        out(line)
    check = window_check(model, closed, window, WINDOW_CHECKS)
    if check is not None:
        out("")
        out(
            f"The live feed keeps {check['window']} candles. The same {check['checked']} forecasts made from that "
            f"window alone differ at the 4th close by {check['mean_abs_difference']:.3f} on average "
            f"(largest {check['max_abs_difference']:.3f}); the direction call changed in {check['direction_changed']}."
        )
    stored = {
        "format": FILE_FORMAT,
        "model": model,
        "skill": evaluation.skill,
        "evaluation": {
            "blocks": evaluation.blocks,
            "scored_blocks": evaluation.scored_blocks,
            "comparable_train": fc.COMPARABLE_TRAIN,
            # For the record only, never shown as the model's record: the closed hours of the
            # scored fits, and every forecast of every fit as it was scored before.
            "closed_market": evaluation.closed_market,
            "all_forecasts": evaluation.all_forecasts,
            "min_train": fc.MIN_TRAIN,
            "block": fc.BLOCK,
            "gap": fc.HORIZON,
            "mae_by_step": list(evaluation.mae_by_step),
            "baseline_mae_by_step": list(evaluation.baseline_mae_by_step),
            "up_share": evaluation.up_share,
            "hit_rate_floor": evaluation.hit_floor,
            "mae_gain_floor": evaluation.mae_gain_floor,
            "live_window": check,
        },
        "candles": {"from": first, "to": last, "count": int(len(closed)), "holes": len(gaps), "missing": int(missing)},
        "source": {"name": source.name, "symbol": source.symbol},
        "trained_at": int(clock()),
    }
    check_file(stored)  # what the web app will accept
    write_model(out_path, stored)
    out("")
    out(f"Final model fitted on {model['rows']} rows (ridge strength {model['ridge']}), written to {out_path}")
    out("Restart the web app to use it.")
    return 0


def main(argv: list[str] | None = None, *, source: DataSource | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=SOURCES, default=settings.price_source)
    parser.add_argument("--out", type=Path, default=Path(settings.forecast_model_path), help="model file to write")
    args = parser.parse_args(argv)
    if args.source not in SOURCES:  # argparse does not check a default against choices
        parser.error(f"unknown price source {args.source!r}; choose one of {', '.join(SOURCES)}")
    if settings.signal_timeframe != fc.TIMEFRAME:
        parser.error(f"the forecast is made for {fc.TIMEFRAME} candles; the signal timeframe is {settings.signal_timeframe}")
    if source is None:
        try:
            source = make_source(settings, args.source)
        except DataSourceError as error:
            print(f"STOPPED: {error}")
            return 1
    return train(source, args.out, window=settings.history_candles)


if __name__ == "__main__":
    raise SystemExit(main())
