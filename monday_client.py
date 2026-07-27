"""
monday_client.py
-----------------
Minimal, resilient GraphQL client for monday.com's v2 API.

Design choices (see DECISION_LOG.md for full reasoning):
- Direct GraphQL calls instead of MCP: fewer moving parts, full control over
  retry/backoff, no dependency on a third-party-hosted MCP server that is
  itself subject to the same rate limits everyone else is hitting.
- Simple in-memory TTL cache keyed by (query, variables): most founder
  questions don't need up-to-the-second data, and caching is the single
  biggest lever against 429s during a high-traffic hackathon window.
- Exponential backoff honoring monday.com's `Retry-After` header when present,
  falling back to a standard exponential schedule otherwise.
- Column value parsing normalizes monday.com's JSON column values (status,
  date, numbers, dropdown, text, people, etc.) into plain Python values,
  and treats missing/empty column values as None rather than raising.
"""

import os
import time
import json
import hashlib
import requests
from typing import Any, Optional

MONDAY_API_URL = "https://api.monday.com/v2"
DEFAULT_CACHE_TTL_SECONDS = int(os.environ.get("MONDAY_CACHE_TTL", "120"))
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.5


class MondayAPIError(Exception):
    pass


class MondayClient:
    def __init__(self, api_token: Optional[str] = None, cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS):
        self.api_token = api_token or os.environ.get("MONDAY_API_TOKEN")
        if not self.api_token:
            raise MondayAPIError(
                "MONDAY_API_TOKEN not set. Export it or put it in a .env file."
            )
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---------- low-level request handling ----------

    def _cache_key(self, query: str, variables: dict) -> str:
        raw = json.dumps({"q": query, "v": variables}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def execute(self, query: str, variables: Optional[dict] = None, use_cache: bool = True) -> dict:
        variables = variables or {}
        key = self._cache_key(query, variables)

        if use_cache and key in self._cache:
            ts, cached_result = self._cache[key]
            if time.time() - ts < self.cache_ttl:
                return cached_result

        headers = {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
        }

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    MONDAY_API_URL,
                    json={"query": query, "variables": variables},
                    headers=headers,
                    timeout=30,
                )
            except requests.RequestException as e:
                last_error = e
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BASE_BACKOFF_SECONDS * (2 ** attempt)))
                time.sleep(retry_after)
                last_error = MondayAPIError("Rate limited (429)")
                continue

            if resp.status_code >= 500:
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
                last_error = MondayAPIError(f"Server error {resp.status_code}")
                continue

            try:
                data = resp.json()
            except ValueError:
                raise MondayAPIError(f"Non-JSON response: {resp.text[:300]}")

            if "errors" in data:
                # monday.com sometimes returns complexity/rate-limit errors as
                # GraphQL errors rather than HTTP 429s.
                err_text = json.dumps(data["errors"])
                if "rate limit" in err_text.lower() or "complexity" in err_text.lower():
                    time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
                    last_error = MondayAPIError(err_text)
                    continue
                raise MondayAPIError(err_text)

            if use_cache:
                self._cache[key] = (time.time(), data["data"])
            return data["data"]

        raise MondayAPIError(f"Exceeded retries. Last error: {last_error}")

    def clear_cache(self):
        self._cache.clear()

    # ---------- column value normalization ----------

    @staticmethod
    def parse_column_value(col: dict) -> Optional[str]:
        """
        Normalize a monday.com column_value object into a plain, human-readable
        string. Returns None for genuinely empty values so downstream code can
        distinguish "no data" from "0" or "false".
        """
        text = col.get("text")
        if text is None or text == "":
            return None
        return text

    # ---------- board/item fetching ----------

    def get_board_schema(self, board_id: str) -> dict:
        query = """
        query ($boardId: [ID!]) {
          boards(ids: $boardId) {
            id
            name
            columns {
              id
              title
              type
            }
          }
        }
        """
        data = self.execute(query, {"boardId": [board_id]})
        boards = data.get("boards", [])
        return boards[0] if boards else {}

    def get_board_items(self, board_id: str, limit: int = 500) -> list[dict]:
        """
        Fetch all items on a board with their column values, normalized into
        flat dicts: {"name": ..., "<column title>": value_or_None, ...}
        Handles pagination via monday.com's cursor-based next_items_page.
        """
        schema = self.get_board_schema(board_id)
        columns = schema.get("columns", [])
        col_titles = {c["id"]: c["title"] for c in columns}

        query = """
        query ($boardId: ID!, $limit: Int!, $cursor: String) {
          boards(ids: [$boardId]) {
            items_page(limit: $limit, cursor: $cursor) {
              cursor
              items {
                id
                name
                column_values {
                  id
                  text
                }
              }
            }
          }
        }
        """

        items_out = []
        cursor = None
        while True:
            data = self.execute(query, {"boardId": board_id, "limit": limit, "cursor": cursor})
            boards = data.get("boards", [])
            if not boards:
                break
            page = boards[0]["items_page"]
            for item in page["items"]:
                row = {"name": item.get("name")}
                for cv in item["column_values"]:
                    title = col_titles.get(cv["id"], cv["id"])
                    row[title] = self.parse_column_value(cv)
                items_out.append(row)
            cursor = page.get("cursor")
            if not cursor:
                break
        return items_out
