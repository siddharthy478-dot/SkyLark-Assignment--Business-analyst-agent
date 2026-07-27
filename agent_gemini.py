"""
agent_gemini.py
----------------
Gemini-powered replacement for agent_claude.py. Same job: answer
founder-level business questions by dynamically querying monday.com (via
monday_client) at question time, using function calling so the model
decides which board(s) to pull and reasons over the returned rows itself.

Why this file exists instead of editing agent_claude.py in place:
- agent_claude.py is kept as the reference/original implementation (see
  DECISION_LOG.md "Model swap" entry). main.py picks one via a single
  import line -- see GEMINI_MIGRATION.md for the exact change.

Wire-format differences from the Anthropic version (this is the whole
reason this couldn't just be a find-and-replace):
- Anthropic uses roles "user"/"assistant" with "tool_use"/"tool_result"
  content blocks. Gemini uses roles "user"/"model" with "functionCall"/
  "functionResponse" parts, and takes a separate top-level
  "system_instruction" field instead of a "system" string.
- Anthropic tool schemas use JSON Schema with lowercase types ("object",
  "string"). Gemini's function_declarations use OpenAPI-style schema with
  UPPERCASE types ("OBJECT", "STRING").
- Anthropic returns tool_use blocks with "input"; Gemini returns
  functionCall parts with "args".
- functionResponse.response must be a JSON *object*, not a raw string, so
  tool output (already a JSON string from _run_tool) is parsed back into
  a dict before being embedded.

The conversation `history` this module produces/consumes is stored in
Gemini's own `contents` format. The frontend (static/index.html) treats
history as an opaque blob it round-trips verbatim, so this format does not
need to match agent_claude.py's history format -- no conversion layer
needed when swapping.
"""

import os
import json
import requests
from monday_client import MondayClient, MondayAPIError

# Free-tier model as of writing. If you hit quota errors or this model is
# retired, check https://ai.google.dev/gemini-api/docs/models for the
# current free-tier-eligible model name and update this constant.
MODEL = "gemini-flash-latest"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
)

WORK_ORDERS_BOARD_ID = os.environ.get("WORK_ORDERS_BOARD_ID", "")
DEALS_BOARD_ID = os.environ.get("DEALS_BOARD_ID", "")

# Identical wording to agent_claude.py on purpose -- the answer-style and
# clarifying-question behavior is a product decision, not a model-specific
# one, and should stay consistent regardless of which LLM is behind it.
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
        "function_declarations": [
            {
                "name": "get_work_orders",
                "description": (
                    "Fetch all rows from the Work Orders board (project "
                    "execution data), with normalized column values. Use "
                    "for questions about operational metrics, project "
                    "status, delivery performance, timelines."
                ),
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "get_deals",
                "description": (
                    "Fetch all rows from the Deals board (sales pipeline "
                    "data), with normalized column values. Use for "
                    "questions about revenue, pipeline, sector performance, "
                    "deal stages."
                ),
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "get_board_schema",
                "description": (
                    "Fetch the column names/types for a board, without row "
                    "data. Use this first if you're unsure what columns "
                    "exist (e.g. to check whether a 'sector' or 'industry' "
                    "column exists before answering a sector-specific "
                    "question)."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "board": {
                            "type": "STRING",
                            "enum": ["work_orders", "deals"],
                        }
                    },
                    "required": ["board"],
                },
            },
        ]
    }
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
        # Surface API problems to the model as data, so it can explain the
        # limitation to the founder instead of the whole request failing.
        return json.dumps({"error": f"monday.com API error: {e}"})


class GeminiAPIError(Exception):
    pass


def _call_gemini(contents: list, api_key: str) -> dict:
    try:
        resp = requests.post(
            GEMINI_API_URL,
            params={"key": api_key},
            headers={"content-type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": contents,
                "tools": TOOLS,
            },
            timeout=60,
        )
    except requests.RequestException as e:
        raise GeminiAPIError(f"Could not reach Gemini API: {e}")

    if resp.status_code != 200:
        # Surface the actual response body -- this is where "invalid API
        # key", "model not found", "quota exceeded", etc. show up in plain
        # text, instead of letting raise_for_status() throw a generic
        # exception that bubbles up as an unhandled 500.
        raise GeminiAPIError(
            f"Gemini API returned {resp.status_code}: {resp.text[:500]}"
        )

    return resp.json()


def answer_question(user_message: str, history: list, monday: MondayClient, api_key: str) -> tuple[str, list]:
    """
    history: list of Gemini `contents` entries ({"role": "user"|"model",
    "parts": [...]}) from prior turns, passed through client-side -- same
    stateless-history decision as agent_claude.py, see DECISION_LOG.md.
    Returns (reply_text, updated_history).
    """
    contents = history + [{"role": "user", "parts": [{"text": user_message}]}]

    for _ in range(5):  # cap tool-use loop iterations as a safety valve
        try:
            result = _call_gemini(contents, api_key)
        except GeminiAPIError as e:
            return f"Error calling Gemini: {e}", contents

        candidates = result.get("candidates", [])
        if not candidates:
            # e.g. blocked by safety filters, or an empty/error response
            reason = result.get("promptFeedback", {}).get("blockReason", "unknown")
            return f"The model returned no response (reason: {reason}).", contents

        content = candidates[0].get("content", {"role": "model", "parts": []})
        parts = content.get("parts", [])
        contents.append({"role": "model", "parts": parts})

        function_calls = [p for p in parts if "functionCall" in p]
        if not function_calls:
            text_parts = [p["text"] for p in parts if "text" in p]
            reply = "\n".join(text_parts).strip()
            return reply, contents

        function_response_parts = []
        for part in function_calls:
            fc = part["functionCall"]
            name = fc["name"]
            args = fc.get("args", {})
            output_json = _run_tool(monday, name, args)
            try:
                output_obj = json.loads(output_json)
            except json.JSONDecodeError:
                output_obj = {"raw": output_json}
            function_response_parts.append({
                "functionResponse": {
                    "name": name,
                    "response": {"content": output_obj},
                }
            })
        contents.append({"role": "user", "parts": function_response_parts})

    return (
        "Sorry, I couldn't finish reasoning about that in time -- try rephrasing or narrowing the question.",
        contents,
    )
