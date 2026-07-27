# Decision Log — Founder Insights Agent

## Tech stack & why
- **Backend:** Python + FastAPI. Chosen for speed of implementation and because
  it maps cleanly onto a single `/chat` endpoint plus a static UI — no need
  for a heavier framework at this scope.
- **Frontend:** One static HTML/JS page (no build step, no framework). A
  hackathon judge just needs a working chat link; a full SPA framework buys
  nothing here and costs setup time.
- **LLM:** Claude API (`claude-sonnet-4-6`) via direct `/v1/messages` calls
  with tool use (function calling), not LangChain or another agent framework.
  Tool use natively supports the "let the model decide which data to pull and
  reason over it" pattern this task needs, so an extra framework layer would
  only add indirection.
- **Monday.com connection: direct GraphQL API, not MCP.** The rate-limit
  warnings during development came from browsing the monday.com web app
  itself (many hackathon participants logged in simultaneously), not from
  API traffic — so API rate limiting isn't actually the driving reason here.
  The real reasons for choosing direct API over MCP: fewer moving parts (no
  dependency on a third-party-hosted MCP server), and full control over our
  own retry/backoff and caching logic, which is simpler to reason about and
  debug than delegating that layer to someone else's server. The retry/
  backoff/caching in `monday_client.py` is kept regardless, as reasonable
  defensive practice against any API's standard per-minute limits — not
  because of the web-app traffic issue reported.

## Key assumptions made
1. **Board IDs are static once the CSVs are imported.** The agent reads them
   from environment variables rather than discovering them dynamically by
   name — simpler and avoids an extra API call on every request. Trade-off:
   if boards are renamed/recreated, the env vars must be updated manually.
2. **Full-board snapshot querying, not query translation.** Rather than
   building a layer that turns "energy sector this quarter" into a
   monday.com column-filter GraphQL query, the agent fetches the whole board
   (cached) and lets Claude reason over the rows in-context. This trades
   some token/latency cost for dramatically broader question coverage with
   much less code — the right trade at hackathon scope, but wouldn't scale
   to boards with tens of thousands of rows (see "What I'd do differently").
3. **Conversation history is stateless/client-side.** The frontend passes the
   full message history back on every request rather than the server
   maintaining a session store. Simplest possible implementation with no
   database; the trade-off is no persistence across page reloads or multiple
   devices.
4. **Column value parsing uses monday.com's `text` field** for every column
   type (status, date, dropdown, numbers, etc.) rather than type-specific
   parsing (e.g. structured date objects). This is simpler and handles nulls
   uniformly, at the cost of losing some structured precision (e.g. sorting
   dates as strings rather than real date objects). Flagged as a caveat the
   agent can surface if asked to do precise date math.
5. **"Leadership updates" — explicitly out of scope.** The task description
   asks the log to explain "how you interpreted 'leadership updates,'" but
   this was treated as an optional additional requirement beyond the core
   feature set. Decision: not implemented, to keep focus on the core
   requirements (import, connection, preprocessing, LLM Q&A, hosting). The
   general answer-style guidance already in `agent.py`'s system prompt
   (headline takeaway, supporting numbers, caveats last) is good
   communication practice on its own merits, but no dedicated "leadership
   updates" feature was built. This is a deliberate scope cut, not an
   oversight.

## Trade-offs chosen and why
- **Caching over real-time-always:** a 120s TTL cache trades a small amount
  of staleness for fewer API calls and faster repeat answers. Not a response
  to any observed API rate limiting (that wasn't actually happening — see
  above) — just sensible default hygiene. Configurable via
  `MONDAY_CACHE_TTL` if judges want to see live updates during a demo (set
  to 0/low value beforehand).
- **Broad tool-use reasoning over precise query-building:** covers more
  question types out of the box; costs more tokens per query and would need
  revisiting for large boards or tight latency requirements.
- **No auth/rate-limiting on the hosted endpoint itself:** acceptable for a
  hackathon demo link; not production-ready as-is (see below).
- **No persistent datastore:** nothing is written back to monday.com or
  stored elsewhere; the agent is read-only by design, which keeps scope
  tight and avoids any risk of the agent mutating real board data.

## What I'd do differently with more time
- Add real column-level GraphQL filtering (e.g. monday.com's `items_page`
  with `query_params`) for boards too large to fit in context, instead of
  always fetching the full board.
- Add a lightweight structured-date parser so the agent can do reliable
  date-range math ("this quarter") instead of relying on Claude to parse
  text-formatted dates.
- Add a session store (even just a dict keyed by session id) so
  conversation history survives page reloads without the client needing to
  resend it.
- Add basic auth or a shareable-but-unguessable link token before treating
  this as anything beyond a demo.
- Write a small eval set of sample founder questions with expected
  answer characteristics, to catch regressions as the prompt/tools evolve.
- Revisit the optional "leadership updates" requirement if time allows,
  once the core feature set is confirmed solid.

## Data quality handling
- Missing/null column values are normalized to `None` and the agent is
  instructed to explicitly call out incomplete data rather than silently
  treating it as zero or omitting it.
- The agent is instructed to state confidence caveats (small sample size,
  stale-looking dates, obviously inconsistent naming) inline in its answers
  rather than in a separate report.

## Model swap: Claude → Gemini
- **Reason:** free tier. No task requirement drove this — Anthropic API
  usage would have consumed paid credits; Google AI Studio's Gemini API
  free tier covers this workload at hackathon scale.
- **What was kept:** agent_claude.py is left in place, untouched, as the
  original/reference implementation. agent_gemini.py is a full parallel
  reimplementation, not a wrapper around the Claude version — the two
  APIs' function-calling wire formats (tool_use/tool_result vs.
  functionCall/functionResponse, system string vs. system_instruction,
  lowercase vs. UPPERCASE JSON schema types) are different enough that a
  thin adapter would have been more code than a clean rewrite.
- **What stayed identical on purpose:** the system prompt wording (answer
  style, clarifying-question behavior, "leadership update" framing), the
  three tools (get_work_orders / get_deals / get_board_schema), and the
  tool-use loop's bounded-iteration safety valve. These are product
  decisions, not model-specific ones, and swapping the LLM shouldn't
  change the product behavior.
- **Trade-off:** raw REST via `requests` again (matching the Claude
  version's style) rather than the `google-genai` SDK, to keep
  dependencies minimal and the two implementations easy to compare
  side-by-side.
- **Conversation history format is not shared** between the two agent
  modules (Anthropic-shaped vs. Gemini-shaped `contents`/messages). This is
  safe because the frontend treats `history` as an opaque blob it just
  round-trips — but it does mean history is not portable if you switch
  agent modules mid-conversation (starting a new chat session after
  switching is fine; resuming an old one against the other agent module is
  not supported).