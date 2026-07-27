# Founder Insights Agent

A chat agent that answers founder-level business questions (revenue, pipeline
health, sector performance, operations) by dynamically querying two
monday.com boards — **Work Orders** (project execution) and **Deals** (sales
pipeline) — using Gemini for reasoning and tool use.

Live demo: _add your Render URL here once deployed_

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python + FastAPI | One `/chat` endpoint + static file serving; no heavier framework needed at this scope |
| Frontend | Single static HTML/JS page | No build step; a hackathon judge just needs a working chat link |
| LLM | Gemini API (`gemini-flash-latest`), raw REST via `requests` | Free tier; raw REST keeps dependencies minimal instead of adding an SDK |
| monday.com | Direct GraphQL API via `requests` | Full control over retry/backoff/caching; no third-party MCP server dependency |
| Hosting | Render (free web service) | No credit card, live public URL, auto-deploy from GitHub |

Full reasoning for each choice, plus trade-offs and assumptions, is in
[`DECISION_LOG.md`](./DECISION_LOG.md). The Gemini migration specifically is
covered in [`GEMINI_MIGRATION.md`](./GEMINI_MIGRATION.md).

---

## Repo structure

```
.
├── main.py              # FastAPI app: routes, env loading, wiring
├── agent_gemini.py       # Active agent: Gemini tool-use loop (current)
├── agent_claude.py       # Reference implementation: original Claude version (kept, not used)
├── monday_client.py      # GraphQL client: retry/backoff, caching, schema + item fetching
├── static/
│   └── index.html         # Chat UI (dark mode, cold-start warning, no build step)
├── requirements.txt
├── .env.example           # Copy to .env and fill in real values
├── .gitignore              # Excludes .env from version control
├── DECISION_LOG.md          # Assumptions, trade-offs, what to do with more time
├── GEMINI_MIGRATION.md       # Claude → Gemini swap notes
└── README.md                  # This file
```

---

## How it works internally

### 1. Data layer — `monday_client.py`
A thin GraphQL client wrapping monday.com's `/v2` API:
- **Retry/backoff**: honors `Retry-After` on 429s, exponential backoff on
  5xx/network errors, up to `MAX_RETRIES`.
- **Caching**: in-memory TTL cache (`MONDAY_CACHE_TTL`, default 120s) keyed
  by query+variables, so repeated questions in a short window don't re-hit
  the API.
- **Schema + items fetching**: `get_board_schema()` returns column
  definitions; `get_board_items()` paginates through all rows via
  `items_page`/cursor and flattens each item into `{"name": ..., "<column
  title>": value_or_None, ...}`.
- **Null-safe parsing**: every column value is read from monday.com's
  `text` field and normalized to `None` when empty, so missing data is
  distinguishable from `"0"` or `false` downstream.

### 2. Reasoning layer — `agent_gemini.py`
This is the core "LLM interface" the task asks for. It exposes three tools
to Gemini via function calling:

- `get_work_orders` — full Work Orders board, for operational questions.
- `get_deals` — full Deals board, for revenue/pipeline questions.
- `get_board_schema` — column names/types only, for the model to check
  whether a filter concept (e.g. "sector") actually exists as a column
  before answering.

**The flow for one chat turn:**
1. `answer_question()` appends the user's message to the running
   conversation (`contents`, in Gemini's own message-history shape).
2. Calls Gemini's `generateContent` endpoint with the system prompt, tools,
   and full conversation so far.
3. If Gemini responds with a `functionCall` (it wants data), the matching
   tool runs against `monday_client`, and the JSON result is fed back to
   Gemini as a `functionResponse`.
4. This repeats (capped at 5 iterations as a safety valve) until Gemini
   responds with plain text instead of a function call — that text is the
   final answer returned to the frontend.

**Why full-board-snapshot instead of query translation:** rather than
building a layer that turns "energy sector this quarter" into a
monday.com column-filter query, the whole board (cached) is handed to
Gemini and it reasons over the rows in-context. Broader question coverage,
less code, at the cost of not scaling to very large boards — see
`DECISION_LOG.md`.

**System prompt behavior** (same wording regardless of which LLM is
plugged in — this is a product decision, not model-specific):
- Never guesses at numbers without pulling live data first.
- Asks one clarifying question for ambiguous terms (e.g. "this quarter"),
  stating a best-guess fallback in the same message.
- Answers like a leadership brief: headline takeaway → key numbers → data
  quality caveats — not a raw table dump.
- Explicitly states when data is missing/incomplete rather than silently
  omitting or refusing.
- Synthesizes across both boards when a question spans them.

### 3. API layer — `main.py`
- `load_dotenv()` runs before any other imports, since `monday_client.py`
  and `agent_gemini.py` read board IDs and tokens from the environment at
  **import time**, not inside functions.
- `GET /` serves the chat UI. `GET /health` is a basic healthcheck (also
  useful as an uptime-ping target to prevent Render free-tier cold starts).
- `POST /chat` takes `{message, history}`, runs `answer_question()`, and
  returns `{reply, history}`. History is round-tripped through the client
  on every request — no server-side session store (see `DECISION_LOG.md`).

### 4. Frontend — `static/index.html`
Single page, no framework, no build step. Notable behavior: if the first
request of a session takes longer than 5 seconds, the loading message
switches to an explanation of Render's free-tier cold start (with a live
elapsed-time counter) instead of leaving the user staring at a generic
spinner.

---

## Setup & running locally

### 1. Clone and enter the repo
```
git clone <your-repo-url>
cd <repo-folder>
```

### 2. Create a virtual environment (recommended)
```
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\Activate.ps1
```

### 3. Install dependencies
```
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Configure environment variables
```
cp .env.example .env
```
Then fill in `.env`:
- `MONDAY_API_TOKEN` — monday.com Admin → API.
- `WORK_ORDERS_BOARD_ID` / `DEALS_BOARD_ID` — numeric IDs from each board's
  URL, after importing the two CSVs manually.
- `GEMINI_API_KEY` — from **https://aistudio.google.com/app/apikey**
  (free, no credit card).
- `MONDAY_CACHE_TTL` — optional, defaults to 120 seconds.

**Never commit `.env`** — it's already covered by `.gitignore`.

### 5. Run the server
```
uvicorn main:app --reload
```
Open **http://localhost:8000**.

---

## Testing

There's no automated test suite (out of scope at hackathon speed — see
"what I'd do differently" in `DECISION_LOG.md`). To manually verify the
agent end-to-end:

1. **Env sanity check** — confirm all four required variables load:
   ```
   python -c "from dotenv import load_dotenv; import os; load_dotenv(); print({k: bool(os.environ.get(k)) for k in ['MONDAY_API_TOKEN','GEMINI_API_KEY','WORK_ORDERS_BOARD_ID','DEALS_BOARD_ID']})"
   ```
2. **List models your key can call** (Gemini's model lineup changes
   frequently; useful if `MODEL` in `agent_gemini.py` ever 404s again):
   ```
   python -c "
   import os, requests
   from dotenv import load_dotenv
   load_dotenv()
   r = requests.get('https://generativelanguage.googleapis.com/v1beta/models', headers={'x-goog-api-key': os.environ['GEMINI_API_KEY']})
   for m in r.json().get('models', []):
       if 'generateContent' in m.get('supportedGenerationMethods', []):
           print(m['name'])
   "
   ```
3. **Run the server** (`uvicorn main:app --reload`) and ask real questions
   through the UI at `localhost:8000`, e.g.:
   - "How many open deals do we have?"
   - "How's our pipeline looking for [a sector in your data] this quarter?"
   - "Compare our closed deals to project delivery performance."

   Confirm the agent actually calls a tool (visible as a brief delay before
   the reply, since it's hitting monday.com + Gemini) rather than
   hallucinating an answer.
4. **Data quality check** — ask a question touching a column you know has
   nulls/inconsistent values in your CSV, and confirm the agent calls out
   the caveat rather than silently guessing.

---

## Deploying (Render)

1. Push this repo to GitHub (`.env` excluded).
2. **render.com** → sign up (no card required) → **New → Web Service** →
   connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add the same four environment variables from `.env` in Render's
   **Environment** tab.
5. Deploy — you get a public URL. Every push to the connected branch
   auto-redeploys.

Render's free tier sleeps after 15 minutes of inactivity (cold start ~30–60s
on the next request) — the frontend's cold-start warning message handles
this gracefully. To keep it warm during a demo/grading window, ping
`/health` every ~10–14 minutes with a free service like UptimeRobot.

---

## Known limitations

See `DECISION_LOG.md` for full reasoning. Summary:
- No column-level server-side filtering — full board fetched every time
  (cached). Fine for small/medium boards.
- No structured date parsing — relies on the model reasoning over
  text-formatted dates from monday.com.
- No server-side session store — conversation history round-trips through
  the client.
- No auth on the `/chat` endpoint — acceptable for a demo link, not for
  anything beyond.
- "Leadership updates" (from the task description) was interpreted as an
  answer-style requirement (see system prompt above), not a separate
  feature — deliberate scope cut, documented in `DECISION_LOG.md`.
