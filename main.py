"""Forecast a launch day's Twitch audience, hour by hour, from its first hour.

The idea: no launch is the first of its kind. Every big game that came before
left a full release-day curve in the case file (the comparables), and those
curves, rescaled by their own first hour, all tell the same rough story: the
audience dips while the world sleeps, then climbs as each region wakes and
unlocks, peaking in the US evening. So this program does not guess a shape
from nothing. It reads the comparables, turns each one into a launch-day
profile (the log of each hour's viewers over the first hour's), and forecasts
each hour from the distribution of those profiles, weighted toward launches
whose first hour was about the size of this one's. The centre of the forecast
is the weighted median profile; the spread comes from how much the profiles
disagree at that hour, which grows as the day goes on.

Only the standard library is used, there is no network access, and nothing
here is random: the same case always yields the same bytes.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The scored levels, in the order each output row lists them.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# Standard normal quantiles at those levels, used for the smooth spread.
Z_SCORES = [-1.645, -0.674, 0.0, 0.674, 1.645]

# How tightly the magnitude kernel hugs the target's first hour (log units).
# A comparable whose first hour is a factor e**BW away counts about half as
# much; one much further away counts for almost nothing.
KERNEL_BW = 0.75
# 0.0 means the quantiles come from a smooth normal spread around the median
# profile, 1.0 means they come straight from the empirical profile quantiles.
# Halfway keeps the skew the comparables really show without letting the two
# or three wildest launches set the tails.
BLEND = 0.5
# Shrink the median profile toward a flat day (0.0 = flat, 1.0 = full profile).
# A little shrinkage guards against the one launch that does not follow the
# pack; the profile is still doing most of the work.
PROFILE_SHRINK = 0.85
# Stretch or squeeze every quantile's distance from the median.
SPREAD_SCALE = 0.9
# Drop launches whose early-access window already peaked before release day;
# their release-day shape is not a launch shape.
DROP_EARLY_ACCESS = True
# A comparable must cover at least this many of the 23 forecast hours.
MIN_HOURS = 12


def _parse_time(text):
    """An ISO-8601 UTC timestamp as an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _hour_offset(start, when):
    """Whole hours from start to when (negative when before start)."""
    return int((_parse_time(when) - _parse_time(start)).total_seconds() // 3600)


def _weighted_quantile(pairs, level):
    """The weighted quantile of (value, weight) pairs at the given level.

    Values are sorted, weights accumulate, and the result interpolates
    linearly between the two values the level falls between. Deterministic.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0])
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        return None
    target = level * total
    seen = 0.0
    previous_value = None
    for value, weight in ordered:
        seen += weight
        if seen >= target:
            if previous_value is None or weight <= 0:
                return value
            over = (seen - target) / weight
            return value - over * (value - previous_value)
        previous_value = value
    return ordered[-1][0]


def _median(pairs):
    return _weighted_quantile(pairs, 0.5)


def _profiles(case):
    """Launch-day profiles from the case's comparables.

    Each profile is (anchor, {hour offset: viewers}) where the anchor is the
    first hour's count and the offsets run 0..23 over the release day. The
    pre-release hype curve is left out: it is not a launch. A profile with
    no usable anchor, or one that covers too little of the day, is left out.
    """
    profiles = []
    for comp in case.get("comparables") or []:
        name = comp.get("name") or ""
        if "before release" in name:
            continue
        unlock = comp.get("unlock") or ""
        if DROP_EARLY_ACCESS and "early-access-contaminated" in unlock:
            continue
        day_start = comp.get("day_start")
        if not day_start:
            continue
        hours = {}
        for point in comp.get("hourly") or []:
            viewers = point.get("viewers")
            if viewers is None:
                continue
            offset = _hour_offset(day_start, point["from"])
            if 0 <= offset <= 23 and viewers > 0:
                hours[offset] = viewers
        anchor = hours.get(0)
        if not anchor:
            continue
        covered = sum(1 for off in range(1, 24) if off in hours)
        if covered < MIN_HOURS:
            continue
        profiles.append((anchor, hours))
    return profiles


def _hour_forecast(p1, profiles, hour):
    """The five quantiles for one forecast hour, as viewer counts."""
    offset = hour - 1
    log_p1 = math.log(p1)
    ratios = []
    for anchor, hours in profiles:
        viewers = hours.get(offset)
        if viewers is None or viewers <= 0:
            continue
        # Closeness in size: launches that opened like this one count most.
        gap = math.log(anchor) - log_p1
        weight = math.exp(-(gap * gap) / (2.0 * KERNEL_BW * KERNEL_BW))
        if weight < 1e-6:
            continue
        ratios.append((math.log(viewers / anchor), weight))
    if not ratios:
        return _flat_forecast(p1, hour)
    centre = _median(ratios) or 0.0
    centre *= PROFILE_SHRINK
    low_q = _weighted_quantile(ratios, 0.25)
    high_q = _weighted_quantile(ratios, 0.75)
    if low_q is None or high_q is None:
        spread = 0.15 * math.sqrt(hour - 1)
    else:
        # Interquartile range turned into a normal spread.
        spread = max(0.05, (high_q - low_q) / 1.349)
    quantiles = []
    for level, z in zip(LEVELS, Z_SCORES):
        smooth = z * spread
        empirical = _weighted_quantile(ratios, level)
        if empirical is None:
            direct = smooth
        else:
            direct = empirical - centre
        shift = (1.0 - BLEND) * smooth + BLEND * direct
        quantiles.append(p1 * math.exp(centre + SPREAD_SCALE * shift))
    return _clean(quantiles)


def _flat_forecast(p1, hour):
    """Fallback when no profile covers the hour: stay near the first hour."""
    spread = 0.15 * math.sqrt(hour - 1)
    return _clean([p1 * math.exp(z * spread) for z in Z_SCORES])


def _clean(quantiles):
    """Clamp to (0, 1e9], round to 2 decimals, keep non-decreasing."""
    out = []
    previous = 0.0
    for value in quantiles:
        if not math.isfinite(value) or value <= 0:
            value = 1.0
        value = min(value, 1e9)
        value = math.floor(value * 100 + 0.5) / 100
        if value <= previous:
            value = previous
        if value <= 0:
            value = 1.0
        previous = value
        out.append(int(value) if float(value).is_integer() else value)
    return out


def forecast_case(case):
    """Forecast one case: {"hours": [{"hour": h, "q": [...]}]} for h 2..24."""
    first = next((row for row in case.get("known") or [] if row.get("hour") == 1), None)
    if first is None:
        raise ValueError("case has no hour-1 print")
    p1 = max(1, first["viewers"])
    profiles = _profiles(case)
    rows = []
    for row in case.get("forecast") or []:
        hour = row["hour"]
        rows.append({"hour": hour, "q": _hour_forecast(p1, profiles, hour)})
    rows.sort(key=lambda row: row["hour"])
    return {"hours": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        result = forecast_case(case)
        (OUTPUT_DIR / (path.stem + ".out")).write_text(
            json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
