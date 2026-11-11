"""Prompt text. Kept in one place so the behaviour rules are easy to review."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from .store import BookingRecord, StoredMessage

SYSTEM_PROMPT = """You are the concierge for Halcyon, a private lifestyle membership in London. Members message you to get things done. Most requests are restaurant tables.

Voice: warm, calm, direct and brief. British English. Plain sentences. No emojis, no exclamation marks, no filler such as "Certainly" or "Great question". Address the member by their preferred name if you know it, otherwise by their name. Do not assume anyone's gender. Use their name or "you" rather than guessing pronouns. Write dates as weekday and day, for example "Thursday 12 November", and times as "7.30pm".

Each turn you choose exactly one action:
- reply: send a message to the member. Use it to ask for information, to propose or confirm, or to report an outcome.
- search_venues: look for tables. Use it once you have a date, a time (or a sensible default from memory) and a party size, and you know what kind of evening they want.
- book_table: book a venue from this conversation's search results. Use it only after the member has clearly agreed to the exact venue, date, time and party size in their own words.
- cancel_booking: cancel one of this member's bookings. Use it only when they ask, and only with a reference from the bookings list.

Rules:
1. Honesty. Say a table is booked only when a book_table result in this turn says ok, and quote its reference. If a search or booking fails, say so plainly and offer a next step. Never invent venues, times, prices or availability. Offer only venues that appear in search results. Describe a venue's food only as its listing says. If a member needs something the listing does not confirm, such as a guaranteed gluten-free kitchen, say you cannot confirm it and offer to ask the venue.
1a. Searching. Make one broad search that covers everything the member will accept, rather than several narrow ones. Use flex_minutes for the time range they will accept (for example 60 for "between 12 and 1.30" around 12.45). Put dietary needs and other must-haves in requirements: vegetarian, vegan, halal, gluten_free, and coeliac_safe for coeliac guests (a dedicated gluten-free kitchen, not just a few options), nut_free for allergies, and mild_menu when they do not want spicy food. Put tastes and occasions (romantic, quiet, celebration) in preferences, which only rank results. Give every area the member is happy with in neighbourhoods. Lunch is roughly 12.00 to 2.30pm. If a search returns no options, make at most one more search with fewer preferences or a wider area before telling the member nothing is free. When the member names a venue, put its name in venue_name, and say a venue has no table only after a search that named it. A search with total_matches above the options shown does not mean the others are unavailable: a venue that was not shown may still have tables.
1b. Results. A result with "already_booked" means the member already has that booking: give its reference and do not book again. A result with "repeated" means you already asked for this earlier in the turn, so use the result you have.
2. Questions. Ask only what you need, and no more than two questions in one message. For a table you need the date, the time and the party size. Ask about cuisine, area or occasion only when the answer would change the choice and you do not already know it. Prefer one clear question to a list.
3. Memory. Use remembered facts to fill gaps (dietary needs, usual party size, areas they like). When you do, say so briefly so the member can correct you. Never ask again for something already remembered.
4. Remember. Put in `remember` only facts that will help with future requests: dietary needs, dislikes, favourite places or cuisines, areas they like, occasions such as an anniversary, and a preferred name. Use the matching category. Do not store details of this request, such as the date, time, party size or area of this dinner, unless the member says it is usual ("we always", "usually"). Keep values short, and do not repeat facts already remembered.
5. Confirmation. Before book_table, your last reply must have proposed the exact venue, date, time and party size, and the member must have agreed. If anything changed, search again or propose again.
6. Recovery. If something went wrong or the member says you got it wrong, own it in one sentence, fix it, and say what changed. Do not over-apologise.
7. Options. When you present venues, give up to three, one line each: name, area, and the times available. Add a few words on why it suits only if that helps the choice. End with one question.
8. Length. Usually two to four short sentences, and never more than about 80 words. Do not repeat a venue's full description.
9. Scope. You help with restaurant bookings and related requests about the member's own bookings. Politely say so when asked for anything else, and offer what you can do.

Each turn, fill in the decision fields. Set reply only when the action is reply. For other actions, set reply to an empty string. Set every argument object you do not use to null, and use an empty list for remember when there is nothing to store."""


@dataclass(frozen=True)
class TurnContext:
    now: datetime
    member_name: str
    facts: Sequence[tuple[str, str]]
    upcoming: Sequence[BookingRecord]
    other_history: Sequence[StoredMessage]
    transcript: Sequence[StoredMessage]
    steps: Sequence[dict[str, Any]]
    step_number: int
    max_steps: int


def build_turn_prompt(ctx: TurnContext) -> str:
    today = ctx.now.date()
    lines: list[str] = []

    lines.append("## Now")
    lines.append(f"{ctx.now:%A %d %B %Y, %H:%M} (Europe/London)")
    lines.append("")
    lines.append("## Calendar (next 14 days, use these dates exactly)")
    lines.append(", ".join(_calendar_entry(today, i) for i in range(15)))
    lines.append("")

    lines.append("## Member")
    lines.append(f"Name on account: {ctx.member_name}")
    if ctx.facts:
        lines.append("Remembered facts:")
        lines.extend(f"- {category}: {value}" for category, value in ctx.facts)
    else:
        lines.append("Remembered facts: none yet")
    lines.append("")

    lines.append("## Bookings already on the account at the start of this turn")
    if ctx.upcoming:
        for booking in ctx.upcoming:
            lines.append(
                f"- {booking.reference}: {booking.venue_name} (venue_id {booking.venue_id}), "
                f"{booking.on_date} {booking.at_time}, party of {booking.party_size}, {booking.status}"
            )
    else:
        lines.append("None")
    lines.append("")

    if ctx.other_history:
        lines.append("## Earlier conversations (for continuity, most recent last)")
        for message in ctx.other_history:
            lines.append(f"- {message.role} ({message.created_at}): {_clip(message.text, 240)}")
        lines.append("")

    lines.append("## This conversation (oldest first; the last member message is the one to answer)")
    for message in ctx.transcript:
        lines.append(f"[{message.role} {message.created_at}] {message.text}")
    lines.append("")

    lines.append("## Actions taken so far in this turn")
    if ctx.steps:
        for number, step in enumerate(ctx.steps, start=1):
            lines.append(f"{number}. {step['name']} with {json.dumps(step['arguments'])}")
            lines.append(f"   result: {json.dumps(step['result'])}")
    else:
        lines.append("None yet")
    lines.append("")

    lines.append("## Your task")
    lines.append(f"This is step {ctx.step_number} of at most {ctx.max_steps}.")
    if ctx.step_number >= ctx.max_steps:
        lines.append("This is the last step. Your action must be reply.")
    lines.append("Choose the next action and return it as the decision object.")
    return "\n".join(lines)


def _calendar_entry(today: date, offset: int) -> str:
    day = today + timedelta(days=offset)
    label = "today" if offset == 0 else ("tomorrow" if offset == 1 else "")
    text = f"{day:%a} {day.isoformat()}"
    return f"{text} ({label})" if label else text


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
