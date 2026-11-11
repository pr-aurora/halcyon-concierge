"""Conversation handling: one turn per member message.

A turn is a short loop. The model chooses one action per step (reply, search,
book, or cancel). Tool actions run against the venue service and their results go
back into the next step's prompt. The turn ends when the model replies, or when the
step limit or time budget runs out, in which case the member gets an honest fallback.

Turns for one conversation run one at a time, so replies stay in order. Turns for
different conversations run in parallel.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from .brain import Brain, BrainError, Decision
from .config import Settings
from .prompts import SYSTEM_PROMPT, TurnContext, build_turn_prompt
from .store import BookingRecord, Store, StoredMessage
from .timeutil import london_now
from .venues import VenueService

log = logging.getLogger("halcyon.concierge")

PER_CALL_TIMEOUT_S = 60.0
MIN_RETRY_BUDGET_S = 15.0
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
FALLBACK_TEXT = (
    "I'm sorry, I ran into a problem on my side and haven't been able to finish that. "
    "Could you send it again in a moment?"
)


@dataclass
class Turn:
    conversation_id: str
    message_id: str
    member_id: str
    seq: int
    deadline: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    booked_references: list[str] = field(default_factory=list)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


class Concierge:
    def __init__(self, *, store: Store, venues: VenueService, brain: Brain, settings: Settings) -> None:
        self._store = store
        self._venues = venues
        self._brain = brain
        self._settings = settings
        self._pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="halcyon-turn")
        self._conversation_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # Entry points --------------------------------------------------------

    def receive(self, *, conversation_id: str, message_id: str, member_id: str, member_name: str, text: str) -> bool:
        """Store an inbound member message and schedule a turn. Returns False for a duplicate."""
        stored = self._store.add_member_message(
            conversation_id=conversation_id,
            message_id=message_id,
            member_id=member_id,
            member_name=member_name,
            text=text,
        )
        if stored is None:
            log.info("duplicate message %s ignored", message_id)
            return False
        self._pool.submit(self._run_turn, conversation_id, message_id)
        return True

    def conversation_messages(self, conversation_id: str) -> list[StoredMessage]:
        return self._store.conversation_messages(conversation_id)

    def recover_unanswered(self) -> None:
        """Resume member messages that were received but not answered before a restart."""
        pending = self._store.unanswered_member_messages()
        for message in pending:
            log.info("resuming unanswered message %s", message.id)
            self._pool.submit(self._run_turn, message.conversation_id, message.id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    # Turn ----------------------------------------------------------------

    def _run_turn(self, conversation_id: str, message_id: str) -> None:
        with self._lock_for(conversation_id):
            message = self._store.message_by_id(message_id)
            if message is None:
                log.error("message %s vanished before its turn ran", message_id)
                return
            turn = Turn(
                conversation_id=conversation_id,
                message_id=message_id,
                member_id=message.member_id,
                seq=message.seq,
                deadline=time.monotonic() + self._settings.turn_budget_s,
            )
            try:
                self._take_turn(turn)
            except Exception:
                log.exception("turn failed for message %s", message_id)
                self._fallback(turn)

    def _take_turn(self, turn: Turn) -> None:
        started = time.monotonic()
        snapshot = self._snapshot(turn)
        for step in range(1, self._settings.max_steps + 1):
            if turn.remaining() <= MIN_RETRY_BUDGET_S:
                break
            decision = self._decide(turn, snapshot, step)
            for fact in decision.remember:
                self._store.remember(turn.member_id, fact["category"], fact["value"])

            if decision.action == "reply":
                if not decision.reply:
                    log.warning("empty reply on step %d for %s", step, turn.message_id)
                    break
                self._send(turn, decision.reply)
                log.info("turn %s replied on step %d in %.1fs", turn.message_id, step, time.monotonic() - started)
                return

            name = decision.action
            arguments = _arguments_for(decision)
            repeat = _earlier_identical_step(turn.steps, name, arguments)
            if repeat is not None:
                # The model asked for the same action again in this turn. Reuse the result, do not act twice.
                result = {**repeat["result"], "repeated": True}
            else:
                result = self._execute(turn, name, arguments)
                self._store.log_tool_call(
                    conversation_id=turn.conversation_id,
                    message_id=turn.message_id,
                    name=name,
                    arguments=arguments,
                    result=result,
                )
            turn.steps.append({"name": name, "arguments": arguments, "result": result})

            # A confirmed booking or cancellation is fully described by the venue's answer, so
            # report it directly. This saves a model call and cannot drift from the real outcome.
            confirmation = _confirmation_text(name, result)
            if confirmation and repeat is None:
                self._send(turn, confirmation)
                log.info("turn %s confirmed %s in %.1fs", turn.message_id, name, time.monotonic() - started)
                return

        log.warning("turn %s used all steps in %.1fs without replying", turn.message_id, time.monotonic() - started)
        self._fallback(turn)

    def _decide(self, turn: Turn, snapshot: TurnContext, step: int) -> Decision:
        context = replace(
            snapshot,
            steps=list(turn.steps),
            step_number=step,
            max_steps=self._settings.max_steps,
        )
        prompt = build_turn_prompt(context)
        last_error: BrainError | None = None
        for attempt in (1, 2):
            budget = min(PER_CALL_TIMEOUT_S, turn.remaining())
            if budget <= 0 or (attempt == 2 and budget < MIN_RETRY_BUDGET_S):
                break
            try:
                return self._brain.decide(system=SYSTEM_PROMPT, prompt=prompt, timeout_s=budget)
            except BrainError as exc:
                last_error = exc
                log.warning("decision attempt %d failed on step %d: %s", attempt, step, exc)
        raise last_error or BrainError("no time left for a decision")

    def _snapshot(self, turn: Turn) -> TurnContext:
        """What the member and their conversation looked like when this turn started."""
        now = london_now()
        return TurnContext(
            now=now,
            member_name=self._store.member_name(turn.member_id) or "Member",
            facts=self._store.facts(turn.member_id),
            upcoming=self._store.bookings_for_member(turn.member_id, on_or_after=now.date().isoformat()),
            other_history=self._store.other_conversation_messages(turn.member_id, turn.conversation_id),
            transcript=self._store.messages_before(turn.conversation_id, turn.seq),
            steps=[],
            step_number=0,
            max_steps=self._settings.max_steps,
        )

    # Tools ---------------------------------------------------------------

    def _execute(self, turn: Turn, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_venues":
            return self._search(arguments)
        if name == "book_table":
            return self._book(turn, arguments)
        return self._cancel(turn, arguments)

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        on = _parse_date(args.get("date"))
        if on is None:
            return _error("date_required", "A valid date (YYYY-MM-DD) is needed to search.")
        party_size = args.get("party_size")
        if not isinstance(party_size, int) or party_size < 1:
            return _error("party_size_required", "A party size is needed to search.")
        time_hhmm = args.get("time")
        if time_hhmm and not TIME_RE.match(time_hhmm):
            return _error("bad_time", "Time must be HH:MM, 24-hour.")
        flex = args.get("flex_minutes")
        if flex is not None and not (isinstance(flex, int) and 0 <= flex <= 180):
            flex = None
        result = self._venues.search(
            on=on,
            party_size=party_size,
            time_hhmm=time_hhmm or None,
            flex_minutes=flex if time_hhmm else None,
            cuisine=args.get("cuisine") or None,
            neighbourhoods=[str(a) for a in args.get("neighbourhoods") or []],
            price_max=args.get("price_max") or None,
            requirements=list(args.get("requirements") or []),
            preferences=list(args.get("preferences") or []),
        )
        if result.error:
            return _error(result.error.code, result.error.message)
        options = [
            {
                "venue_id": offer.venue.id,
                "name": offer.venue.name,
                "cuisine": offer.venue.cuisine,
                "neighbourhood": offer.venue.neighbourhood,
                "price_band": offer.venue.price_band,
                "tags": list(offer.venue.tags),
                "matched_preferences": list(offer.matched_preferences),
                "summary": offer.venue.summary,
                "times": list(offer.times),
            }
            for offer in result.offers
        ]
        outcome: dict[str, Any] = {"ok": True, "date": on.isoformat(), "options": options}
        if not options:
            outcome["note"] = "No venues match these details with a free table."
        return outcome

    def _book(self, turn: Turn, args: dict[str, Any]) -> dict[str, Any]:
        on = _parse_date(args.get("date"))
        venue_id = args.get("venue_id")
        time_hhmm = args.get("time")
        party_size = args.get("party_size")
        if not venue_id or on is None or not time_hhmm or not TIME_RE.match(time_hhmm):
            return _error("incomplete_booking", "A venue, date, time and party size are needed.")
        if not isinstance(party_size, int) or party_size < 1:
            return _error("incomplete_booking", "A party size is needed.")

        existing = self._store.active_booking_at(turn.member_id, venue_id, on.isoformat(), time_hhmm)
        if existing is not None:
            # Same member, venue and time already booked. Return it rather than booking twice.
            return {
                "ok": True,
                "already_booked": True,
                "reference": existing.reference,
                "venue_name": existing.venue_name,
                "date": existing.on_date,
                "time": existing.at_time,
                "party_size": existing.party_size,
            }

        member_name = self._store.member_name(turn.member_id) or "Member"
        outcome = self._venues.book(
            venue_id=venue_id,
            on=on,
            time_hhmm=time_hhmm,
            party_size=party_size,
            guest_name=member_name,
            special_requests=args.get("special_requests") or None,
        )
        if not outcome.ok or outcome.reference is None:
            return _error(outcome.error.code, outcome.error.message) if outcome.error else _error("failed", "Booking failed.")

        venue = self._venues.venue(venue_id)
        venue_name = venue.name if venue else venue_id
        self._store.add_booking(
            reference=outcome.reference,
            member_id=turn.member_id,
            conversation_id=turn.conversation_id,
            venue_id=venue_id,
            venue_name=venue_name,
            on_date=on.isoformat(),
            at_time=time_hhmm,
            party_size=party_size,
        )
        turn.booked_references.append(outcome.reference)
        return {
            "ok": True,
            "reference": outcome.reference,
            "venue_name": venue_name,
            "date": on.isoformat(),
            "time": time_hhmm,
            "party_size": party_size,
            # Only what the venue actually recorded. Say a request was passed on only if it appears here.
            "special_requests_recorded": args.get("special_requests") or None,
        }

    def _cancel(self, turn: Turn, args: dict[str, Any]) -> dict[str, Any]:
        reference = args.get("booking_ref")
        booking: BookingRecord | None = (
            self._store.booking_for_member(turn.member_id, reference) if reference else None
        )
        if booking is None:
            return _error("not_found", "No booking with that reference belongs to this member.")
        member_name = self._store.member_name(turn.member_id) or "Member"
        outcome = self._venues.cancel(reference, guest_name=member_name)
        if not outcome.ok:
            if outcome.error and outcome.error.code == "already_cancelled":
                self._store.set_booking_status(reference, "cancelled")
            return _error(outcome.error.code, outcome.error.message) if outcome.error else _error("failed", "Cancellation failed.")
        self._store.set_booking_status(reference, "cancelled")
        return {"ok": True, "reference": reference, "venue_name": booking.venue_name, "status": "cancelled"}

    # Output --------------------------------------------------------------

    def _send(self, turn: Turn, text: str) -> StoredMessage:
        return self._store.add_assistant_message(
            conversation_id=turn.conversation_id,
            member_id=turn.member_id,
            text=text,
        )

    def _fallback(self, turn: Turn) -> None:
        if self._store.has_assistant_reply_after(turn.conversation_id, turn.seq):
            return
        text = FALLBACK_TEXT
        if turn.booked_references:
            refs = ", ".join(turn.booked_references)
            text = (
                f"I'm sorry, I ran into a problem on my side before I could finish replying. "
                f"Your booking reference is {refs}. Could you tell me again what else you need?"
            )
        self._send(turn, text)
        log.warning("sent fallback for message %s", turn.message_id)

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._conversation_locks.get(conversation_id)
            if lock is None:
                lock = threading.Lock()
                self._conversation_locks[conversation_id] = lock
            return lock


def _confirmation_text(name: str, result: dict[str, Any]) -> str | None:
    """A plain confirmation for a successful booking or cancellation, or None."""
    if not result.get("ok"):
        return None
    if name == "book_table":
        when = _friendly_date(result["date"])
        if result.get("already_booked"):
            return (
                f"You already have a table at {result['venue_name']} on {when} at {_friendly_time(result['time'])} "
                f"for {result['party_size']}. The reference is {result['reference']}."
            )
        text = (
            f"Your table at {result['venue_name']} is booked for {when} at {_friendly_time(result['time'])}, "
            f"for {result['party_size']}. Your reference is {result['reference']}."
        )
        note = (result.get("special_requests_recorded") or "").strip().rstrip(".!?")
        if note:
            text += f" We have passed on your note: {note}."
        return text + " Is there anything else I can arrange?"
    if name == "cancel_booking":
        return f"Your booking at {result['venue_name']} ({result['reference']}) is cancelled. Is there anything else I can help with?"
    return None


def _friendly_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%A %-d %B")


def _friendly_time(hhmm: str) -> str:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    suffix = "am" if hours < 12 else "pm"
    hours = hours % 12 or 12
    return f"{hours}.{minutes:02d}{suffix}" if minutes else f"{hours}{suffix}"


def _arguments_for(decision: Decision) -> dict[str, Any]:
    if decision.action == "search_venues":
        return decision.search or {}
    if decision.action == "book_table":
        return decision.booking or {}
    return decision.cancel or {}


def _earlier_identical_step(steps: list[dict[str, Any]], name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    for step in steps:
        if step["name"] == name and step["arguments"] == arguments:
            return step
    return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}
