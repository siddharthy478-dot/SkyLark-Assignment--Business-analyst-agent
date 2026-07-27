import os
from dotenv import load_dotenv

# Must run before importing monday_client / agent_gemini below: both modules
# read board IDs and other settings from the environment at import time
# (module-level os.environ.get(...) calls), not inside functions -- so .env
# has to be loaded first or those reads silently return "" / None.
load_dotenv()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from monday_client import MondayClient, MondayAPIError
from agent_gemini import answer_question

app = FastAPI(title="Monday.com Founder Insights Agent")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

_monday_client = None


def get_monday_client() -> MondayClient:
    global _monday_client
    if _monday_client is None:
        _monday_client = MondayClient()
    return _monday_client


class ChatRequest(BaseModel):
    message: str
    history: list = []


class ChatResponse(BaseModel):
    reply: str
    history: list


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not GEMINI_API_KEY:
        return ChatResponse(
            reply="Server misconfiguration: GEMINI_API_KEY is not set.",
            history=req.history,
        )
    try:
        monday = get_monday_client()
    except MondayAPIError as e:
        return ChatResponse(reply=f"Server misconfiguration: {e}", history=req.history)

    reply, updated_history = answer_question(req.message, req.history, monday, GEMINI_API_KEY)
    return ChatResponse(reply=reply, history=updated_history)


app.mount("/static", StaticFiles(directory="static"), name="static")
