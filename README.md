# Halcyon concierge

An assistant service for Halcyon members. It holds a conversation with each member, finds restaurant tables, books them through a mock venue service, and remembers what it learns about each member across conversations.

Public repository: https://github.com/pr-aurora/halcyon-concierge
Agent URL: https://lightweight-expected-available-mystery.trycloudflare.com

## Running it

```
PORT=8411 AGENT_TOKEN=... ./start.sh
```

- Python 3.14 standard library only. There is nothing to install.
- Without `ANTHROPIC_API_KEY`, the LLM is `claude -p` with `claude-haiku-5-5`. The `claude` binary must be on `PATH`, or set `CLAUDE_BIN`.
- With `ANTHROPIC_API_KEY`, the service calls the Messages API directly. This path is written but not yet exercised against the live API.
- State lives in `data/` (`halcyon.db` for the concierge, `venues.db` for the mock venue service). It is gitignored. Delete it to reset.
- Tests: `python3 -m unittest discover -s tests -t .` (27 tests, about one second, no network).

## How it works

Each member message is stored and acknowledged with 202 straight away. A worker then runs one **turn**:

1. The model sees the member's remembered facts, their bookings, recent history from their other conversations, this conversation's transcript, and any results from earlier steps in this turn.
2. It returns exactly one structured decision: `reply`, `search_venues`, `book_table` or `cancel_booking`. It can also list new facts to remember.
3. Tool actions run against the venue service. Their results go into the next step. A turn ends with a reply, after at most four steps, or when its 120-second budget runs out.
4. If the model fails or runs out of steps, the member gets an honest fallback. If a booking was already made, the fallback quotes its reference.

Turns for one conversation run one at a time, so replies stay in order. Turns for different conversations run in parallel.

Files:

| File | Purpose |
| --- | --- |
| `halcyon/api.py` | HTTP contract, bearer auth, validation |
| `halcyon/concierge.py` | Turn loop, tool execution, fallback |
| `halcyon/brain.py` | Decision schema, `claude -p` and Anthropic API backends |
| `halcyon/prompts.py` | System prompt and per-turn context |
| `halcyon/store.py` | SQLite: messages, members, memory, bookings, audit log |
| `halcyon/venues.py` | Mock venue service with its own SQLite file |
| `halcyon/mock_data/venues.json` | 29 fictional London restaurants |
| `tests/test_service.py` | Contract, booking flow and venue rules |

## Decisions

- **One structured decision per model call.** A single JSON schema covers every action, so the code never parses free text. The schema is enforced by `--json-schema` on the CLI and by a forced tool call on the API.
- **Stateless calls with full context.** Each call carries everything it needs, which suits `claude -p` and keeps each turn reproducible. Member memory is read once at the start of a turn, so the model is not confused by its own new booking.
- **Memory keyed by member id, not conversation.** Facts have categories (dietary, likes, dislikes, neighbourhood, occasion, party, preferred name). They are de-duplicated and capped at 200 characters each. The prompt tells the model not to store details of a single request. The last 12 messages from the member's other conversations also go into the prompt.
- **Requirements versus preferences.** Dietary needs and allergies are hard filters. Tastes such as "romantic" only rank results, so a single unmet taste never empties the search.
- **Searches cover a window.** `flex_minutes` lets one search cover a range of times, and `neighbourhoods` takes several areas. A search can also name one venue. Its answer then says why there is no table (closed that day, party too large, or no free seating), and the result reports `total_matches`, so the model never treats a venue that was merely left out of the top three as unavailable.
- **Confirmations come from the venue.** A successful booking is confirmed with a fixed sentence built from the venue's response. That saves a model call and cannot drift from the real outcome. Cancellations are not templated, so the model can offer a replacement booking in the same turn.
- **Idempotency.** A `message_id` is stored once, so a redelivered message gets 202 and no second reply. Within one turn, a repeated action reuses the earlier result. A booking for the same member, venue and time returns the existing reference rather than booking again.
- **Honesty about outcomes.** A booking is reported only after a successful result, and the reference is quoted from the venue's response. Special requests are echoed back from the venue, so the model can only say they were passed on if they were recorded. The venue listings carry specific tags, such as `coeliac_safe` for a dedicated gluten-free kitchen, and the model is told to say when a listing does not confirm a need.
- **Mock venues behave like a third party.** Each seating is released to the provider about 70% of the time, decided by a hash of venue, date and seating, so runs are repeatable. Covers per seating, closed days, maximum party size and a 60-day booking window are all enforced. Bookings are real rows in a separate database.
- **Timezone and dates.** Turns use Europe/London. The prompt gives the model a 14-day calendar so it does not have to work out weekdays. Timestamps are UTC ISO 8601 with a `Z` suffix.
- **Failure budget.** Each model call has a 60-second timeout, with one retry if at least 15 seconds remain. The turn budget is 120 seconds, which leaves margin under the member's 180-second limit.

## Assumptions

- The guest name on a booking is the account name.
- Members can only cancel bookings that are on their own account.
- Changing a booking is done by cancelling and rebooking. There is no amend.
- There is no payment, deposit or cancellation fee.
- The venue catalogue is fictional and covers the London areas used in the practice scenarios.

## Known gaps

- **Booking confirmation is enforced by the prompt only.** A booking needs the member's agreement in the conversation, and the model is told this. Code does not check it. A pending-booking state would make this explicit.
- **Latency is the main weakness.** Each model call takes about 4 to 7 seconds through the CLI, most of it start-up. A turn that searches and then replies takes about 12 to 20 seconds, and a turn that needs three or four steps can take 30 seconds. Members wait about 15 seconds before they answer, so this shows. The Anthropic API path should be faster; I have not measured it.
- **Model calls can time out.** In the first practice run one call hit the 60-second limit, and the retry succeeded. The retry is the only protection.
- **Catalogue coverage.** Requests for areas or needs with few venues, such as Islington with a coeliac-safe kitchen, end in an honest "no", not a good option. The catalogue is mock data, so this reflects my choices, not the venue network.
- **Memory is not reviewed.** Facts are added but never corrected or removed unless the member contradicts them in a later message, which is not handled yet.
- **Replies are sometimes long.** The prompt asks for two to four sentences, but option lists often run past it. Code does not check length.
- **No prompt caching.** Every turn sends the full context to the model.

## What I would do next

1. Add an explicit pending-booking state so that a booking can only be made after a proposal that the member has accepted.
2. Send a short acknowledgement when a search is in flight, so members never wait long in silence.
3. Switch to the Anthropic API once a key is available. Measure it against the CLI path.
4. Add amend and correction flows. When a member says something was wrong, update the stored fact and tell them what changed.
5. Build a small evaluation set from practice transcripts, and score the rules: honesty, confirmation, and repeated deliveries.
6. Add structured logs and metrics for turn time, step count, and fallback rate.

## Time spent

Roughly 2 hours, including building the mock service, live checks, and practice runs.

## Things to look at beyond the code

- **Deadline discrepancy.** The brief gives 18 November, 09:45 London time. The portal shows 15:25. I have followed the brief's recommendation of about four hours.
- **Local network in my sandbox.** This machine blocks outbound TCP connections from my tools to localhost, so the HTTP tests drive the real handler in memory rather than through a socket. The live smoke test and practice runs went through the public tunnel.
- **Live behaviour.** The three-turn live check and the practice transcripts are the best evidence of how the assistant behaves. The unit tests check the rules, not the wording.
