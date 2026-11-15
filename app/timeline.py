"""DST-aware timeline core.

This module deliberately only depends on the Python standard library so that
the core algorithm can be unit-tested (and audited) without the web stack.

Problem statement
-----------------
An entrance counter emits records keyed by a monotonically increasing *sample
id*.  On the autumn DST transition day in ``America/New_York`` the clock jumps
from 02:00 EDT back to 01:00 EST, so every wall-clock time in the half-open
interval ``[01:00, 02:00)`` happens twice.  A naive local timestamp therefore
maps to one or two possible UTC instants.

A *legal timeline* is an assignment of one UTC instant to every sample such
that larger sample ids always have strictly larger UTC instants.  A sample's
UTC is *determined* only when it is the same in every legal timeline.

Greedy bounds
-------------
For each candidate (one or two UTC instants per sample, ordered):

* forward pass  -> the earliest UTC that keeps the prefix consistent
* backward pass -> the latest  UTC that keeps the suffix consistent

The range [earliest, latest] describes every value the variable can take in
some legal timeline: the value sets are ordered intervals over a totally
ordered chain and the all-earliest / all-latest greedy solutions are both
feasible, hence identical bounds mean a unique value across *all* legal
timelines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class Record:
    """One counter reading."""

    sample_id: int
    local_time: str          # naive wall-clock time, ISO-8601, no offset
    pass_count: int

    def key(self) -> tuple[int, str, int]:
        return (self.sample_id, self.local_time, self.pass_count)


def parse_local(value: str) -> datetime:
    """Parse a naive local timestamp. Offset-bearing values are rejected."""

    text = value.strip()
    dt = datetime.fromisoformat(text)  # raises ValueError on malformed input
    if dt.tzinfo is not None:
        raise ValueError(
            "local_time must be a naive America/New_York wall-clock time "
            "(no UTC offset / timezone suffix)"
        )
    return dt


def utc_candidates(local_time: str) -> list[datetime]:
    """All UTC instants the naive New York wall-clock time may denote.

    Returns one instant for unambiguous times and two (fold=0 first, i.e. the
    EDT pass, fold=1 second, i.e. the EST pass) inside the repeated hour.
    """

    naive = parse_local(local_time)
    seen: set[datetime] = set()
    out: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=NEW_YORK, fold=fold)
        utc = aware.astimezone(UTC)
        if utc not in seen:
            seen.add(utc)
            out.append(utc)
    return out


@dataclass
class TimelineResult:
    feasible: bool
    earliest: dict[int, datetime]
    latest: dict[int, datetime]

    def is_determined(self, sample_id: int) -> bool:
        return self.earliest[sample_id] == self.latest[sample_id]


def analyze_timeline(records: list[Record]) -> TimelineResult:
    """Compute feasible bounds for every sample id.

    The input may contain duplicate sample ids; the caller (service layer) is
    responsible for deciding whether duplicates conflict.  Here every sample
    id is evaluated with its own candidate set.
    """

    ordered = sorted(records, key=lambda r: r.sample_id)
    ids = [r.sample_id for r in ordered]
    candidates = {r.sample_id: utc_candidates(r.local_time) for r in ordered}

    # Forward pass: smallest candidate strictly greater than the previous one.
    earliest: dict[int, datetime] = {}
    prev: datetime | None = None
    feasible = True
    for sid in ids:
        chosen = None
        for cand in candidates[sid]:  # already sorted ascending
            if prev is None or cand > prev:
                chosen = cand
                break
        if chosen is None:
            feasible = False
            break
        earliest[sid] = chosen
        prev = chosen

    if not feasible:
        return TimelineResult(False, {}, {})

    # Backward pass: largest candidate strictly smaller than the next one.
    latest: dict[int, datetime] = {}
    nxt: datetime | None = None
    for sid in reversed(ids):
        chosen = None
        for cand in reversed(candidates[sid]):
            if nxt is None or cand < nxt:
                chosen = cand
                break
        if chosen is None:  # pragma: no cover - forward feasibility implies this
            feasible = False
            break
        latest[sid] = chosen
        nxt = chosen

    return TimelineResult(feasible, earliest, latest if feasible else {})
