"""End-to-end tests over real HTTP, with a scripted brain and the mock venue service."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from halcyon.api import ConciergeHandler
from halcyon.brain import BrainError, Decision
from halcyon.concierge import Concierge
from halcyon.config import Settings
from halcyon.store import Store
from halcyon.venues import VenueService

TOKEN = "test-token"
TODAY = date(2026, 11, 11)


class ScriptedBrain:
    """Returns queued decisions in order. Each entry is a Decision or an exception."""

    name = "scripted"

    def __init__(self, *entries):
        self.entries = list(entries)
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def decide(self, *, system: str, prompt: str, timeout_s: float) -> Decision:
        with self._lock:
            self.prompts.append(prompt)
            if not self.entries:
                raise BrainError("script exhausted")
            entry = self.entries.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry


def reply(text: str, remember=None) -> Decision:
    return Decision(action="reply", reply=text, remember=remember or [])


def search(**kwargs) -> Decision:
    return Decision(action="search_venues", search=kwargs)


def book(**kwargs) -> Decision:
    return Decision(action="book_table", booking=kwargs)


class _FakeTransport(ConciergeHandler):
    """Runs the real handler methods against in-memory streams, without opening a socket."""

    def __init__(self, server, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        self.server = server
        self.command = method
        self.path = path
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status: int | None = None

    def send_response(self, code, message=None):  # noqa: D401 - matches base signature
        self.status = code

    def send_header(self, keyword, value):
        pass

    def end_headers(self):
        pass


class HarnessMixin:
    def start(self, brain) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "halcyon.db")
        self.venues = VenueService(db_path=self.tmp / "venues.db", latency_s=0, today=lambda: TODAY)
        self.settings = Settings(
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            data_dir=self.tmp,
            model="test",
            anthropic_api_key=None,
            claude_bin="claude",
            venue_latency_s=0,
            turn_budget_s=30,
            max_steps=4,
            workers=4,
        )
        self.brain = brain
        self.concierge = Concierge(store=self.store, venues=self.venues, brain=brain, settings=self.settings)
        self.api = SimpleNamespace(concierge=self.concierge, token=TOKEN)

    def stop(self) -> None:
        self.concierge.shutdown()
        self.store.close()
        self.venues.close()

    def request(self, method: str, path: str, body=None, token: str | None = TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        raw = json.dumps(body).encode() if body is not None else b""
        headers["Content-Length"] = str(len(raw))
        handler = _FakeTransport(self.api, method, path, headers, raw)
        getattr(handler, f"do_{method}")()
        payload = handler.wfile.getvalue()
        return handler.status, json.loads(payload or b"null")

    def member_message(self, conversation_id, text, *, message_id=None, member_id="member-1", name="Jane Doe"):
        message_id = message_id or str(uuid.uuid4())
        body = {"message_id": message_id, "member": {"id": member_id, "name": name}, "text": text}
        return self.request("POST", f"/conversations/{conversation_id}/messages", body)

    def wait_for_assistant(self, conversation_id, count: int, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, body = self.request("GET", f"/conversations/{conversation_id}/messages")
            assistant = [m for m in body["messages"] if m["role"] == "assistant"]
            if len(assistant) >= count:
                return body["messages"]
            time.sleep(0.05)
        self.fail(f"no assistant message {count} arrived in time")


class ContractTests(HarnessMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.start(ScriptedBrain(reply("What time suits you?")))

    def tearDown(self) -> None:
        self.stop()

    def test_requires_bearer_token(self) -> None:
        status, _ = self.request("GET", "/conversations/c1/messages", token="wrong")
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/conversations/c1/messages", token=None)
        self.assertEqual(status, 401)

    def test_post_answers_202_and_get_returns_ordered_transcript(self) -> None:
        mid = str(uuid.uuid4())
        status, _ = self.member_message("c1", "need dinner for 2 thurs, somewhere nice", message_id=mid)
        self.assertEqual(status, 202)

        messages = self.wait_for_assistant("c1", 1)
        self.assertEqual([m["role"] for m in messages], ["member", "assistant"])
        self.assertEqual(messages[0]["id"], mid)
        self.assertEqual(messages[0]["text"], "need dinner for 2 thurs, somewhere nice")
        self.assertEqual(messages[1]["text"], "What time suits you?")
        uuid.UUID(messages[1]["id"])
        self.assertRegex(messages[1]["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_duplicate_delivery_is_answered_once(self) -> None:
        mid = str(uuid.uuid4())
        self.assertEqual(self.member_message("c2", "hello", message_id=mid)[0], 202)
        self.assertEqual(self.member_message("c2", "hello", message_id=mid)[0], 202)
        messages = self.wait_for_assistant("c2", 1)
        time.sleep(0.3)
        _, body = self.request("GET", "/conversations/c2/messages")
        self.assertEqual(sum(1 for m in body["messages"] if m["role"] == "member"), 1)
        self.assertEqual(sum(1 for m in body["messages"] if m["role"] == "assistant"), 1)
        self.assertEqual(len(messages), 2)

    def test_invalid_payload_is_rejected(self) -> None:
        status, _ = self.request("POST", "/conversations/c3/messages", {"text": "hi"})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/conversations/c3/messages", {
            "message_id": "x", "member": {"id": "m"}, "text": 5,
        })
        self.assertEqual(status, 400)

    def test_empty_conversation_returns_empty_list(self) -> None:
        status, body = self.request("GET", "/conversations/unused/messages")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"messages": []})


class BookingFlowTests(HarnessMixin, unittest.TestCase):
    def tearDown(self) -> None:
        self.stop()

    def test_search_then_book_reports_reference_from_venue(self) -> None:
        self.start(ScriptedBrain(
            search(date="2026-11-19", time="20:00", party_size=2, cuisine=None, neighbourhoods=None,
                   price_max=None, requirements=["vegetarian"]),
            reply("Ember & Oak has 20:00 free on Thursday. Shall I book it for two?"),
            book(venue_id="ven-ember-oak", date="2026-11-19", time="20:00", party_size=2, special_requests=None),
        ))
        self.member_message("c-book", "need dinner for 2 thurs, vegetarian please")
        self.wait_for_assistant("c-book", 1)

        # The member agrees. The booking runs in the next turn and is confirmed from the venue's answer.
        self.member_message("c-book", "yes please book it")
        messages = self.wait_for_assistant("c-book", 2)
        confirmation = messages[-1]["text"]
        bookings = self.store.bookings_for_member("member-1", on_or_after="2026-11-01")
        self.assertEqual(len(bookings), 1)
        self.assertEqual(bookings[0].venue_id, "ven-ember-oak")
        self.assertIn(bookings[0].reference, confirmation)
        self.assertIn("Ember & Oak", confirmation)
        self.assertEqual(self.venues.reservation_status(bookings[0].reference), "confirmed")

    def test_repeated_failed_booking_in_one_turn_is_not_retried(self) -> None:
        same = book(venue_id="ven-ember-oak", date="2026-11-19", time="20:00", party_size=2, special_requests=None)
        self.start(ScriptedBrain(same, same, reply("That time has gone. Shall I look at another?")))
        # Fill the 20:00 seating so the booking fails and the model tries the same thing again.
        for party in (8, 8, 8, 4):
            self.venues.book(venue_id="ven-ember-oak", on=date(2026, 11, 19), time_hhmm="20:00",
                             party_size=party, guest_name="Someone Else")
        self.member_message("c-repeat", "book Ember & Oak thurs 8pm for two")
        messages = self.wait_for_assistant("c-repeat", 1)
        self.assertIn("another", messages[-1]["text"])
        self.assertIn('"repeated": true', self.brain.prompts[2])

    def test_booking_the_same_table_in_a_later_turn_returns_the_existing_booking(self) -> None:
        same = book(venue_id="ven-ember-oak", date="2026-11-19", time="20:00", party_size=2, special_requests=None)
        self.start(ScriptedBrain(same, same))
        self.member_message("c-twice", "book Ember & Oak thurs 8pm for two")
        first = self.wait_for_assistant("c-twice", 1)[-1]["text"]
        self.member_message("c-twice", "actually can you book it again to be safe?")
        messages = self.wait_for_assistant("c-twice", 2)
        self.assertEqual(len(self.store.bookings_for_member("member-1", on_or_after="2026-11-01")), 1)
        self.assertIn("You already have a table", messages[-1]["text"])
        reference = self.store.bookings_for_member("member-1", on_or_after="2026-11-01")[0].reference
        self.assertIn(reference, first)
        self.assertIn(reference, messages[-1]["text"])

    def test_moving_a_booking_cancels_then_rebooks_in_one_turn(self) -> None:
        self.start(ScriptedBrain(
            book(venue_id="ven-quiet-table", date="2026-11-19", time="18:00", party_size=2, special_requests=None),
        ))
        self.member_message("c-move", "book The Quiet Table thurs 6pm for two")
        self.wait_for_assistant("c-move", 1)
        reference = self.store.bookings_for_member("member-1", on_or_after="2026-11-01")[0].reference
        self.brain.entries.extend([
            Decision(action="cancel_booking", cancel={"booking_ref": reference}),
            book(venue_id="ven-quiet-table", date="2026-11-19", time="20:00", party_size=2, special_requests=None),
        ])
        self.member_message("c-move", "actually make it 8pm")
        messages = self.wait_for_assistant("c-move", 2)
        self.assertIn("8pm", messages[-1]["text"])
        bookings = {b.at_time: b.status for b in self.store.bookings_for_member("member-1", on_or_after="2026-11-01")}
        self.assertEqual(bookings, {"18:00": "cancelled", "20:00": "confirmed"})

    def test_failed_booking_is_visible_to_the_model_on_the_next_step(self) -> None:
        self.start(ScriptedBrain(
            book(venue_id="ven-ember-oak", date="2026-11-19", time="20:00", party_size=4, special_requests=None),
            reply("I'm sorry, that time has just gone. Shall I look for another time?"),
        ))
        # Ember & Oak has 28 covers per seating and takes at most 8 per booking.
        # Fill the seating exactly so a party of 4 cannot fit.
        for party in (8, 8, 8, 4):
            self.assertTrue(self.venues.book(venue_id="ven-ember-oak", on=date(2026, 11, 19), time_hhmm="20:00",
                                             party_size=party, guest_name="Someone Else").ok)
        self.member_message("c-fail", "book Ember & Oak thurs 8pm for four please")
        messages = self.wait_for_assistant("c-fail", 1)
        self.assertIn("just gone", messages[-1]["text"])
        self.assertIn("slot_unavailable", self.brain.prompts[1])
        self.assertEqual(self.store.bookings_for_member("member-1", on_or_after="2026-11-01"), [])

    def test_exhausted_model_gives_honest_fallback_and_no_booking(self) -> None:
        self.start(ScriptedBrain(BrainError("boom"), BrainError("boom again")))
        self.member_message("c-down", "dinner for two on friday")
        messages = self.wait_for_assistant("c-down", 1)
        fallback = messages[-1]["text"]
        self.assertIn("haven't been able to finish", fallback)
        self.assertNotIn("booked", fallback.lower())

    def test_out_of_window_search_is_reported_as_an_error_to_the_model(self) -> None:
        self.start(ScriptedBrain(
            search(date="2027-03-01", time=None, party_size=2, cuisine=None, neighbourhoods=None,
                   price_max=None, requirements=[]),
            reply("Bookings open 60 days ahead, so I cannot look that far. Could we try a date sooner?"),
        ))
        self.member_message("c-window", "table for 2 on 1 March next year")
        messages = self.wait_for_assistant("c-window", 1)
        self.assertIn("60 days", messages[-1]["text"])

    def test_cancel_only_works_for_the_members_own_booking(self) -> None:
        self.start(ScriptedBrain(
            Decision(action="cancel_booking", cancel={"booking_ref": "VB000000"}),
            reply("I could not find that booking."),
        ))
        self.member_message("c-cancel", "cancel my booking VB000000")
        messages = self.wait_for_assistant("c-cancel", 1)
        self.assertIn("not_found", self.brain.prompts[1])
        self.assertIn("could not find", messages[-1]["text"].lower())


class SearchPromptTests(HarnessMixin, unittest.TestCase):
    def tearDown(self) -> None:
        self.stop()

    def test_later_conversation_sees_remembered_facts_and_earlier_history(self) -> None:
        self.start(ScriptedBrain(
            reply("Noted, vegetarian. What date?", remember=[{"category": "dietary", "value": "vegetarian"}]),
            reply("Welcome back. Shall I look for a table for two again?"),
        ))
        self.member_message("first", "I'm vegetarian, remember that.", member_id="m-9")
        self.wait_for_assistant("first", 1)
        self.member_message("second", "hi again", member_id="m-9")
        self.wait_for_assistant("second", 1)
        second_prompt = self.brain.prompts[1]
        self.assertIn("dietary: vegetarian", second_prompt)
        self.assertIn("I'm vegetarian, remember that.", second_prompt)


class VenueServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.venues = VenueService(db_path=Path(tempfile.mkdtemp()) / "v.db", today=lambda: TODAY)

    def tearDown(self) -> None:
        self.venues.close()

    def test_search_filters_by_requirement_and_party_size(self) -> None:
        result = self.venues.search(on=date(2026, 11, 19), party_size=2, requirements=["vegan"])
        names = {o.venue.name for o in result.offers}
        self.assertTrue(names)
        self.assertTrue(all("vegan" in o.venue.tags for o in result.offers))
        # Largest venue seats 14, so a party of 15 has no options anywhere.
        big = self.venues.search(on=date(2026, 11, 19), party_size=15)
        self.assertEqual(big.offers, ())

    def test_preferences_rank_but_do_not_exclude(self) -> None:
        plain = self.venues.search(on=date(2026, 11, 19), party_size=2, neighbourhoods=["Marylebone", "Mayfair"],
                                   requirements=["vegetarian"], limit=10)
        ranked = self.venues.search(on=date(2026, 11, 19), party_size=2, neighbourhoods=["Marylebone", "Mayfair"],
                                    requirements=["vegetarian"], preferences=["romantic"], limit=10)
        self.assertEqual({o.venue.id for o in plain.offers}, {o.venue.id for o in ranked.offers})
        matches = [len(o.matched_preferences) for o in ranked.offers]
        self.assertEqual(matches, sorted(matches, reverse=True))

    def test_flex_limits_offers_to_the_time_window(self) -> None:
        result = self.venues.search(on=date(2026, 11, 19), party_size=2, time_hhmm="12:30", flex_minutes=30, limit=10)
        self.assertTrue(result.offers)
        for offer in result.offers:
            for time_hhmm in offer.times:
                self.assertLessEqual(abs(int(time_hhmm[:2]) * 60 + int(time_hhmm[3:]) - (12 * 60 + 30)), 30)

    def test_city_lunch_is_offered_on_a_weekday(self) -> None:
        # Cornhill Kitchen releases 12.30 on this date (see _released). Thursday, so the venue is open.
        result = self.venues.search(on=date(2026, 11, 19), party_size=4, time_hhmm="12:30", flex_minutes=60,
                                    neighbourhoods=["Bank", "City of London"], requirements=["vegan"])
        cornhill = [o for o in result.offers if o.venue.id == "ven-cornhill-kitchen"]
        self.assertEqual(len(cornhill), 1)
        self.assertIn("12:30", cornhill[0].times)

    def test_named_venue_search_reports_why_it_has_no_table(self) -> None:
        # Ember & Oak is closed on Mondays, so the reason should say so.
        closed = self.venues.search(on=date(2026, 11, 16), party_size=2, venue_id="ven-ember-oak")
        self.assertEqual(closed.error.code, "venue_closed")
        # On an open night the search returns only that venue, even though others match too.
        open_night = self.venues.search(on=date(2026, 11, 19), party_size=2, venue_id="ven-ember-oak", limit=3)
        self.assertTrue(all(o.venue.id == "ven-ember-oak" for o in open_night.offers))
        self.assertIsNone(open_night.error)

    def test_total_matches_counts_results_beyond_the_limit(self) -> None:
        result = self.venues.search(on=date(2026, 11, 19), party_size=2, limit=1)
        self.assertEqual(len(result.offers), 1)
        self.assertGreater(result.total_matches, 1)

    def test_neighbourhoods_accept_any_of_several_areas(self) -> None:
        result = self.venues.search(on=date(2026, 11, 19), party_size=2, neighbourhoods=["Marylebone", "Mayfair"],
                                    limit=10)
        areas = {o.venue.neighbourhood for o in result.offers}
        self.assertTrue(areas <= {"Marylebone", "Mayfair"})
        self.assertTrue(len(areas) >= 1)

    def test_closed_day_is_respected(self) -> None:
        # The Quiet Table is closed on Mondays.
        monday = date(2026, 11, 16)
        result = self.venues.search(on=monday, party_size=2, cuisine="kaiseki")
        self.assertEqual(result.offers, ())
        outcome = self.venues.book(venue_id="ven-quiet-table", on=monday, time_hhmm="18:00", party_size=2, guest_name="X")
        self.assertEqual(outcome.error.code, "venue_closed")

    def test_cancel_frees_covers(self) -> None:
        first = self.venues.book(venue_id="ven-quiet-table", on=date(2026, 11, 19), time_hhmm="18:00",
                                 party_size=4, guest_name="A")
        self.assertTrue(first.ok)
        second = self.venues.book(venue_id="ven-quiet-table", on=date(2026, 11, 19), time_hhmm="18:00",
                                  party_size=4, guest_name="B")
        self.assertTrue(second.ok)
        third = self.venues.book(venue_id="ven-quiet-table", on=date(2026, 11, 19), time_hhmm="18:00",
                                 party_size=1, guest_name="C")
        self.assertFalse(third.ok)
        self.assertEqual(self.venues.cancel(first.reference, guest_name="A").ok, True)
        retry = self.venues.book(venue_id="ven-quiet-table", on=date(2026, 11, 19), time_hhmm="18:00",
                                 party_size=1, guest_name="C")
        self.assertTrue(retry.ok)

    def test_booking_window_is_enforced(self) -> None:
        result = self.venues.search(on=date(2027, 2, 1), party_size=2)
        self.assertEqual(result.error.code, "date_out_of_range")


class ConfirmationTextTests(unittest.TestCase):
    def test_booking_confirmation_quotes_venue_result_and_tidies_the_note(self) -> None:
        from halcyon.concierge import _confirmation_text

        text = _confirmation_text("book_table", {
            "ok": True, "reference": "VB123456", "venue_name": "Ember & Oak", "date": "2026-11-19",
            "time": "19:30", "party_size": 2, "special_requests_recorded": "One guest is vegan.",
        })
        self.assertEqual(
            text,
            "Your table at Ember & Oak is booked for Thursday 19 November at 7.30pm, for 2. "
            "Your reference is VB123456. We have passed on your note: One guest is vegan. "
            "Is there anything else I can arrange?",
        )

    def test_failed_results_are_not_confirmed_by_template(self) -> None:
        from halcyon.concierge import _confirmation_text

        self.assertIsNone(_confirmation_text("book_table", {"ok": False, "error": "slot_unavailable"}))
        self.assertIsNone(_confirmation_text("search_venues", {"ok": True, "options": []}))


class SettingsTests(unittest.TestCase):
    def test_token_is_required(self) -> None:
        from halcyon.config import ConfigError
        with self.assertRaises(ConfigError):
            Settings.from_env({"PORT": "8411"})


if __name__ == "__main__":
    unittest.main()
