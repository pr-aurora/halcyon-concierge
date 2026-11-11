"""Mock third-party venue booking service.

It stands in for an external reservations provider. It has its own SQLite file and
its own booking references, and the concierge only talks to it through the methods
below, so it could be swapped for an HTTP client later.

Availability has two layers:
  * Base inventory: a deterministic hash of (venue, date, seating) decides whether a
    seating is released to the provider at all. About 30% of seatings are never
    released, which mimics other channels and walk-in holds.
  * Covers: confirmed bookings count against `covers_per_slot`.

The same inputs always give the same answers, so test runs are repeatable.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .timeutil import london_today, utc_iso

CATALOGUE_PATH = Path(__file__).resolve().parent / "mock_data" / "venues.json"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BOOKING_WINDOW_DAYS = 60
TAGS = (
    "vegetarian",
    "vegan",
    "gluten_free",
    "halal",
    "romantic",
    "quiet",
    "private_room",
    "celebration",
    "good_for_groups",
    "late_kitchen",
    "wine_list",
    "counter_seating",
    "dog_friendly",
    "coeliac_safe",
    "nut_free",
    "mild_menu",
)


@dataclass(frozen=True)
class Venue:
    id: str
    name: str
    cuisine: str
    neighbourhood: str
    price_band: int
    max_party: int
    covers_per_slot: int
    tags: tuple[str, ...]
    days_open: tuple[str, ...]
    seatings: tuple[str, ...]
    summary: str

    @classmethod
    def from_dict(cls, data: dict) -> "Venue":
        return cls(
            id=data["id"],
            name=data["name"],
            cuisine=data["cuisine"],
            neighbourhood=data["neighbourhood"],
            price_band=int(data["price_band"]),
            max_party=int(data["max_party"]),
            covers_per_slot=int(data["covers_per_slot"]),
            tags=tuple(data["tags"]),
            days_open=tuple(data["days_open"]),
            seatings=tuple(data["seatings"]),
            summary=data["summary"],
        )

    def open_on(self, on: date) -> bool:
        return WEEKDAYS[on.weekday()] in self.days_open


@dataclass(frozen=True)
class Offer:
    venue: Venue
    times: tuple[str, ...]
    matched_preferences: tuple[str, ...] = ()


@dataclass(frozen=True)
class VenueError:
    code: str
    message: str


@dataclass(frozen=True)
class SearchResult:
    offers: tuple[Offer, ...] = ()
    error: VenueError | None = None


@dataclass(frozen=True)
class BookingResult:
    ok: bool
    reference: str | None = None
    venue_name: str | None = None
    error: VenueError | None = None


@dataclass(frozen=True)
class CancelResult:
    ok: bool
    error: VenueError | None = None


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _released(venue_id: str, on: date, seating: str) -> bool:
    """Deterministic base inventory: roughly 70% of seatings are released."""
    digest = hashlib.sha256(f"{venue_id}|{on.isoformat()}|{seating}".encode()).digest()
    return digest[0] % 10 < 7


class VenueService:
    def __init__(
        self,
        *,
        db_path: Path,
        catalogue_path: Path = CATALOGUE_PATH,
        latency_s: float = 0.0,
        today: Callable[[], date] | None = None,
    ) -> None:
        raw = json.loads(Path(catalogue_path).read_text(encoding="utf-8"))
        self._venues = {v["id"]: Venue.from_dict(v) for v in raw["venues"]}
        self._latency_s = latency_s
        self._today = today or london_today
        self._lock = threading.RLock()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                reference TEXT PRIMARY KEY,
                venue_id TEXT NOT NULL,
                on_date TEXT NOT NULL,
                at_time TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                guest_name TEXT NOT NULL,
                special_requests TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reservations_slot
                ON reservations(venue_id, on_date, at_time, status);
            """
        )
        self._db.commit()

    # Public API ----------------------------------------------------------

    def venue(self, venue_id: str) -> Venue | None:
        return self._venues.get(venue_id)

    def search(
        self,
        *,
        on: date,
        party_size: int,
        time_hhmm: str | None = None,
        cuisine: str | None = None,
        neighbourhoods: Sequence[str] = (),
        price_max: int | None = None,
        requirements: Sequence[str] = (),
        preferences: Sequence[str] = (),
        flex_minutes: int | None = None,
        limit: int = 3,
    ) -> SearchResult:
        """Find tables for a party.

        `requirements` are hard filters (for example dietary needs). `preferences` only
        rank results: venues matching more of them come first, but are never excluded.
        `flex_minutes` limits offers to seatings within that many minutes of `time_hhmm`.
        With no flex, any seating counts and the nearest ones are offered.
        """
        self._pause()
        error = self._check_date(on)
        if error:
            return SearchResult(error=error)
        if party_size < 1:
            return SearchResult(error=VenueError("bad_party_size", "Party size must be at least 1."))
        unknown = [t for t in (*requirements, *preferences) if t not in TAGS]
        if unknown:
            return SearchResult(error=VenueError("unknown_requirement", f"Unknown requirement: {unknown[0]}."))

        target = _minutes(time_hhmm) if time_hhmm else _minutes("19:00")
        areas = [a.lower() for a in neighbourhoods if a]
        ranked: list[tuple[int, int, str, Offer]] = []
        with self._lock:
            for venue in self._venues.values():
                if not venue.open_on(on) or party_size > venue.max_party:
                    continue
                if price_max is not None and venue.price_band > price_max:
                    continue
                if cuisine and cuisine.lower() not in venue.cuisine.lower():
                    continue
                if areas and not any(a in venue.neighbourhood.lower() for a in areas):
                    continue
                if any(r not in venue.tags for r in requirements):
                    continue
                open_times = [
                    t
                    for t in self._open_times(venue, on, party_size)
                    if flex_minutes is None or abs(_minutes(t) - target) <= flex_minutes
                ]
                if not open_times:
                    continue
                nearest = sorted(open_times, key=lambda t: (abs(_minutes(t) - target), t))[:3]
                gap = abs(_minutes(nearest[0]) - target)
                matched = tuple(p for p in preferences if p in venue.tags)
                offer = Offer(venue, tuple(sorted(nearest, key=_minutes)), matched)
                ranked.append((-len(matched), gap, venue.name, offer))

        ranked.sort(key=lambda item: item[:3])
        return SearchResult(offers=tuple(item[3] for item in ranked[:limit]))

    def book(
        self,
        *,
        venue_id: str,
        on: date,
        time_hhmm: str,
        party_size: int,
        guest_name: str,
        special_requests: str | None = None,
    ) -> BookingResult:
        self._pause()
        venue = self._venues.get(venue_id)
        if venue is None:
            return BookingResult(False, error=VenueError("unknown_venue", "No venue with that id."))
        error = self._check_date(on)
        if error:
            return BookingResult(False, error=error)
        if not venue.open_on(on):
            return BookingResult(False, error=VenueError("venue_closed", f"{venue.name} is closed that day."))
        if time_hhmm not in venue.seatings:
            return BookingResult(False, error=VenueError("time_not_offered", f"{venue.name} does not seat at {time_hhmm}."))
        if party_size < 1 or party_size > venue.max_party:
            return BookingResult(False, error=VenueError("party_too_large", f"{venue.name} seats up to {venue.max_party}."))

        with self._lock:
            if not _released(venue.id, on, time_hhmm) or self._covers_left(venue, on, time_hhmm) < party_size:
                return BookingResult(False, error=VenueError("slot_unavailable", "That time has just gone."))
            reference = "VB" + secrets.token_hex(3).upper()
            self._db.execute(
                """INSERT INTO reservations
                   (reference, venue_id, on_date, at_time, party_size, guest_name, special_requests, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)""",
                (
                    reference,
                    venue.id,
                    on.isoformat(),
                    time_hhmm,
                    party_size,
                    guest_name,
                    special_requests,
                    utc_iso(),
                ),
            )
            self._db.commit()
        return BookingResult(True, reference=reference, venue_name=venue.name)

    def cancel(self, reference: str, *, guest_name: str) -> CancelResult:
        self._pause()
        with self._lock:
            row = self._db.execute(
                "SELECT status, guest_name FROM reservations WHERE reference = ?", (reference,)
            ).fetchone()
            if row is None:
                return CancelResult(False, VenueError("not_found", "No reservation with that reference."))
            status, booked_for = row
            if booked_for != guest_name:
                return CancelResult(False, VenueError("not_found", "No reservation with that reference."))
            if status != "confirmed":
                return CancelResult(False, VenueError("already_cancelled", "That reservation is already cancelled."))
            self._db.execute("UPDATE reservations SET status = 'cancelled' WHERE reference = ?", (reference,))
            self._db.commit()
        return CancelResult(True)

    def reservation_status(self, reference: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT status FROM reservations WHERE reference = ?", (reference,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # Internals -----------------------------------------------------------

    def _pause(self) -> None:
        if self._latency_s > 0:
            time.sleep(self._latency_s)

    def _check_date(self, on: date) -> VenueError | None:
        today = self._today()
        if on < today:
            return VenueError("date_in_past", "That date has already passed.")
        if on > today + timedelta(days=BOOKING_WINDOW_DAYS):
            return VenueError("date_out_of_range", f"Bookings open {BOOKING_WINDOW_DAYS} days ahead.")
        return None

    def _covers_left(self, venue: Venue, on: date, time_hhmm: str) -> int:
        booked = self._db.execute(
            """SELECT COALESCE(SUM(party_size), 0) FROM reservations
               WHERE venue_id = ? AND on_date = ? AND at_time = ? AND status = 'confirmed'""",
            (venue.id, on.isoformat(), time_hhmm),
        ).fetchone()[0]
        return venue.covers_per_slot - int(booked)

    def _open_times(self, venue: Venue, on: date, party_size: int) -> Iterable[str]:
        for seating in venue.seatings:
            if _released(venue.id, on, seating) and self._covers_left(venue, on, seating) >= party_size:
                yield seating
