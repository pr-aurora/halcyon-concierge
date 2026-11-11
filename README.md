# Halcyon concierge

An assistant service for Halcyon members. It holds a conversation with each member, finds restaurant tables, books them through a mock venue service, and remembers what it learns about each member across conversations.

- Public repository: https://github.com/pr-aurora/halcyon-concierge
- Agent URL: https://lightweight-expected-available-mystery.trycloudflare.com

## Running it

```
PORT=8411 AGENT_TOKEN=... ./start.sh
```

- Python 3.14 standard library only. Nothing to install.
- Without `ANTHROPIC_API_KEY`, the model is `claude-haiku-5-5` through `claude -p`. The `claude` binary must be on `PATH`, or set `CLAUDE_BIN`.
- With `ANTHROPIC_API_KEY`, the service calls the Messages API directly. This path is written but I have not run it against the live API.
- State is in `data/` (gitignored). Delete it to reset.
- Tests: `python3 -m unittest discover -s tests -t .` (27 tests, about a second, no network).

## How it works

A member message is stored and acknowledged with 202 straight away. A background worker then runs one **turn**:

1. The model sees the member's remembered facts, their bookings, recent history from their other conversations, this conversation's transcript, and the results of earlier steps in this turn.
2. It returns one structured decision: `reply`, `search_venues`, `book_table` or `cancel_booking`, plus any facts to remember.
3. Tool actions run against the venue service, and their results go into the next step. A turn ends with a reply, a confirmed booking, after four steps, or when its 120-second budget runs out.
4. If the model fails or runs out of steps, the member gets an honest fallback. If a booking was made, the fallback quotes its reference.

Turns in one conversation run in order. Turns in different conversations run in parallel.

| File | Purpose |
| --- | --- |
| `halcyon/api.py` | HTTP contract, bearer auth, validation |
| `halcyon/concierge.py` | Turn loop, tool execution, confirmations, fallback |
| `halcyon/brain.py` | Decision schema, `claude -p` and Anthropic API backends |
| `halcyon/prompts.py` | System prompt and per-turn context |
| `halcyon/store.py` | SQLite: messages, members, memory, bookings, audit log |
| `halcyon/venues.py` | Mock venue service with its own SQLite file |
| `halcyon/mock_data/venues.json` | 29 fictional London restaurants |
| `tests/test_service.py` | Contract, booking flow, venue rules, confirmation wording |

## Decisions

- **One structured decision per model call.** One JSON schema covers every action, so the code never parses free text. The CLI enforces it with `--json-schema`, and the API with a forced tool call.
- **Stateless calls with full context.** Each call carries everything it needs. Member memory and bookings are read once at the start of a turn, so the model does not mistake its own new booking for an existing one.
- **Memory is keyed by member id, not conversation.** Facts have categories (dietary, likes, dislikes, neighbourhood, occasion, party, preferred name) and are de-duplicated. The prompt tells the model not to store details of a single request.
- **Hard needs versus tastes.** Dietary needs and allergies are hard filters. Tastes such as "romantic" only rank results.
- **Searches cover a window.** `flex_minutes` covers a range of times, `neighbourhoods` takes several areas, and `venue_name` searches one named venue. A named-venue search says why there is no table (closed that day, party too large, or no free seating). Results report `total_matches`, so a venue left out of the top three is not treated as full.
- **Idempotency.** A `message_id` is stored once, so a redelivery gets 202 and no second reply. A repeated action in one turn reuses its result. A booking for the same member, venue and time returns the existing reference.
- **Confirmations come from the venue.** A successful booking is confirmed with a fixed sentence built from the venue's response. That saves a model call and cannot drift from the outcome. Cancellations are not templated, so the model can offer a replacement booking in the same turn.
- **Honesty about outcomes.** Special requests are echoed back by the venue, so the assistant only says they were passed on when they were recorded. Venue listings carry tags such as `coeliac_safe`, and the model says when a listing does not confirm a need.
- **Mock venues behave like a third party.** About 70% of seatings are released, decided by a hash of venue, date and seating, so runs repeat. Covers per seating, closed days, party limits and a 60-day booking window are enforced. Bookings live in a separate database.
- **Time.** Turns use Europe/London. The model gets a 14-day calendar so it does not work out weekdays. Timestamps are UTC ISO 8601 with a `Z`.
- **Failure budget.** Each model call has a 60-second timeout, with one retry if at least 15 seconds remain. The turn budget is 120 seconds, under the member's 180-second limit.
- **Listen backlog.** The HTTP server's listen backlog is 128. The default of 5 is too small for several clients polling every two seconds.

## Assumptions

- The guest name on a booking is the account name.
- Members can cancel only their own bookings. A change is a cancel followed by a rebooking. There is no amend.
- There is no payment, deposit or cancellation fee.
- The venues are fictional. They cover the London areas used in practice scenarios.

## Practice results

| Run | Result | Notes |
| --- | --- | --- |
| 1 (first build) | 1 of 3 succeeded | Lunch requests failed because the catalogue had no lunch seatings or City venues. Search loops made turns slow. The report also showed one `GET` returning 502 from the tunnel. |
| 2 | 2 of 3 succeeded | Abandoned conversation: a "somewhere new" Islington request with coeliac needs and no coeliac-safe venue there. |
| 3 (final) | 3 of 3 succeeded | Current code. Turns took 6 to 31 seconds. |
| Custom 1 | 1 abandoned | Named venue unavailable. Assistant said a venue "does not appear", which came from truncated results. Fixed with `venue_name` and `total_matches`. |
| Custom 2 | 1 succeeded | Named booking with a note, move to a later time, and a list of bookings. |

The 502 in run 1 is unexplained. The service log showed no errors, and a small listen backlog is the likely cause, which I have since raised. I have not reproduced it.

## Known gaps

- **Confirmation before booking is enforced by the prompt only.** Code does not check that the member agreed. A pending-booking state would make this explicit.
- **Latency is the main weakness.** Each model call takes about 4 to 7 seconds through the CLI, mostly start-up. A search followed by a reply takes 12 to 20 seconds, and three-step turns take up to 30 seconds. Members answer after 15 seconds of silence, so this shows.
- **One model call timed out** at 60 seconds in run 1. The retry covered it, but there is no other protection.
- **Replies are sometimes longer than the four sentences the prompt asks for.** Code does not check length.
- **Memory is never corrected.** Facts are added, but nothing removes or updates them unless the member contradicts them in a later message, and that is not handled.
- **No prompt caching.** Each turn sends the full context.

## What I would do next

1. Add a pending-booking state, so a booking needs a proposal that the member accepts.
2. Measure the Anthropic API path against the CLI. Lower latency is the biggest gain available.
3. Add amend and correction flows, and update stored facts when a member corrects them.
4. Build an evaluation set from practice transcripts, scoring honesty, confirmation and repeated deliveries.
5. Add structured logs and metrics for turn time, step count and fallback rate.

## Time spent

About two hours. That covers the build, live checks, five practice runs and two custom scenarios, and the write-up.

## Things to look at beyond the code

- **Deadline.** The brief gives 18 November, 09:45 London time. The portal shows 15:25. I have kept to the brief's guidance of about four hours.
- **Sandbox networking.** This machine blocks my tools from opening local TCP connections, so the HTTP tests drive the real handler in memory instead of a socket. The live smoke tests and practice runs went through the public tunnel.
- **Model behaviour.** The practice transcripts and custom scenarios show how the assistant behaves. The unit tests check rules, not wording.
