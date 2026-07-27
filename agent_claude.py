"""
agent.py
--------
Claude-powered agent that answers founder-level business questions by
dynamically querying monday.com (via monday_client) at question time.

Design choices (see DECISION_LOG.md):
- Tool-use / function-calling pattern: Claude decides which board(s) to pull
  and reasons over the returned rows itself, rather than us hand-writing a
  query-translation layer for every possible business question. This trades
  some efficiency (we send full board snapshots into context) for much
  broader question coverage with far less code -- the right trade for a
  hackathon scope.
- Clarifying questions: the system prompt explicitly instructs Claude to ask
  a clarifying question when a term in the founder's question is ambiguous
  (e.g. "this quarter", "energy sector" if no column cleanly maps to it)
  instead of silently guessing.
- "Leadership update" framing: answers are instructed to read like a concise
  leadership status update (headline, supporting numbers, caveats) rather
  than a raw data dump -- see DECISION_LOG.md for why this phrase was
  interpreted this way.
"""

import os
import json
import requests
from monday_client import MondayClient, MondayAPIError

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

WORK_ORDERS_BOARD_ID = os.environ.get("WORK_ORDERS_BOARD_ID", "")
DEALS_BOARD_ID = os.environ.get("DEALS_BOARD_ID", "")

SYSTEM_PROMPT = """You are a business analyst assistant for a company founder. \
You answer questions about revenue, sales pipeline, sector performance, and \
operational metrics by querying two live monday.com boards: "Work Orders" \
(project execution data) and "Deals" (sales pipeline data).

Guidelines:
1. Use the provided tools to pull live data before answering. Never guess at \
numbers.
2. If the question is ambiguous (e.g. an undefined time period like "this \
quarter", or a filter that doesn't clearly map to a column in the data), ask \
ONE concise clarifying question instead of guessing silently. State your \
best-guess interpretation as a fallback option in the same message so the \
founder can just confirm instead of re-explaining.
3. Write answers the way you'd brief a founder or leadership team: lead with \
the headline takeaway, back it with the key numbers, then flag any data \
quality caveats (missing fields, small sample size, stale-looking dates, \
etc.) that affect confidence in the answer. Do not just dump a raw table.
4. If data is missing or incomplete for part of the question, say so \
explicitly and answer with whatever is available rather than refusing.
5. When a question spans both boards (e.g. "how do our closed deals compare \
to project delivery performance"), query both and synthesize a single answer.
"""

TOOLS = [
    {
        "name": "get_work_orders",
        "description": (
            "Fetch all rows from the Work Orders board (project execution "
            "data), with normalized column values. Use for questions about "
            "operational metrics, project status, delivery performance, "
            "timelines."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_deals",
        "description": (
            "Fetch all rows from the Deals board (sales pipeline data), with "
            "normalized column values. Use for questions about revenue, "
            "pipeline, sector performance, deal stages."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_board_schema",
        "description": (
            "Fetch the column names/types for a board, without row data. "
            "Use this first if you're unsure what columns exist (e.g. to "
            "check whether a 'sector' or 'industry' column exists before "
            "answering a sector-specific question)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {"type": "string", "enum": ["work_orders", "deals"]}
            },
            "required": ["board"],
        },
    },
]


def _board_id_for(name: str) -> str:
    return WORK_ORDERS_BOARD_ID if name == "work_orders" else DEALS_BOARD_ID


def _run_tool(monday: MondayClient, name: str, tool_input: dict) -> str:
    try:
        if name == "get_work_orders":
            rows = monday.get_board_items(WORK_ORDERS_BOARD_ID)
            return json.dumps(rows)
        if name == "get_deals":
            rows = monday.get_board_items(DEALS_BOARD_ID)
            return json.dumps(rows)
        if name == "get_board_schema":
            board_id = _board_id_for(tool_input["board"])
            schema = monday.get_board_schema(board_id)
            return json.dumps(schema)
        return json.dumps({"error": f"Unknown tool {name}"})
    except MondayAPIError as e:
        # Surface API problems to Claude as data, so it can explain the
        # limitation to the founder instead of the whole request failing.
        return json.dumps({"error": f"monday.com API error: {e}"})


def _call_claude(messages: list, api_key: str) -> dict:
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1500,
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "messages": messages,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def answer_question(user_message: str, history: list, monday: MondayClient, api_key: str) -> tuple[str, list]:
    """
    history: list of {"role": "user"|"assistant", "content": ...} from prior
    turns (kept client-side / passed in each request -- see DECISION_LOG.md
    for why we didn't stand up a session store for this scope).
    Returns (reply_text, updated_history).
    """
    messages = history + [{"role": "user", "content": user_message}]

    for _ in range(5):  # cap tool-use loop iterations as a safety valve
        result = _call_claude(messages, api_key)
        content = result.get("content", [])
        messages.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            reply = "\n".join(text_parts).strip()
            return reply, messages

        tool_results = []
        for tu in tool_uses:
            output = _run_tool(monday, tu["name"], tu.get("input", {}))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": output,
            })
        messages.append({"role": "user", "content": tool_results})

    return "Sorry, I couldn't finish reasoning about that in time -- try rephrasing or narrowing the question.", messages
