import json
import os
import re
import time
import uuid
import threading
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from openai import OpenAI
from databricks.sdk.core import Config
import mlflow

import prompts

# --- Configuration ---
SERVING_ENDPOINT = os.getenv("SERVING_ENDPOINT", "databricks-claude-sonnet-4-5")
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "10"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8192"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))
# Structured output: "json_schema" (Claude/GPT), "json_object" (GPT/Llama), or "none".
RESPONSE_FORMAT = os.getenv("RESPONSE_FORMAT", "json_schema").lower()

# MLflow tracing config.
TRACING_ENABLED = os.getenv("TRACING_ENABLED", "true").lower() == "true"
MLFLOW_EXPERIMENT = os.getenv(
    "MLFLOW_EXPERIMENT", "/Users/stephanie.ross@alchemer.com/steph-test-lm-qa-app-traces"
)

# Default survey passed to the model when the caller doesn't supply one.
DEFAULT_SURVEY_JSON = os.getenv(
    "DEFAULT_SURVEY_JSON",
    json.dumps({"title": "", "pages": [{"page_sku": 1, "questions": []}]}),
)

# OpenAI-compatible client authenticated with the app's service principal.
# Built directly from SDK Config so it works across databricks-sdk versions.
_cfg = Config()


def _get_client() -> OpenAI:
    headers = _cfg.authenticate()
    token = headers["Authorization"].split(" ", 1)[1]
    return OpenAI(base_url=f"{_cfg.host}/serving-endpoints", api_key=token)


def _init_tracing() -> bool:
    """Enable MLflow tracing; failures must never block the app."""
    if not TRACING_ENABLED:
        return False
    try:
        mlflow.set_tracking_uri("databricks")
        mlflow.set_experiment(MLFLOW_EXPERIMENT)
        mlflow.openai.autolog()
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort observability
        print(f"[tracing] disabled: {exc}", flush=True)
        return False


_TRACING = _init_tracing()


class Session:
    def __init__(self) -> None:
        self.messages: List[Dict[str, str]] = []
        self.last_active: float = time.time()


class SessionStore:
    """Thread-safe in-memory session store with TTL-based expiry."""

    def __init__(self, ttl: int, max_turns: int) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max_turns = max_turns

    def _purge_expired(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if now - s.last_active > self._ttl]
        for sid in expired:
            del self._sessions[sid]

    def get_history(self, session_id: str) -> List[Dict[str, str]]:
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            session = self._sessions.get(session_id)
            return list(session.messages) if session else []

    def append(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            session = self._sessions.setdefault(session_id, Session())
            session.messages.append({"role": "user", "content": user_msg})
            session.messages.append({"role": "assistant", "content": assistant_msg})
            # Keep only the last N turns (each turn = user + assistant).
            max_msgs = self._max_turns * 2
            if len(session.messages) > max_msgs:
                session.messages = session.messages[-max_msgs:]
            session.last_active = now

    def reset(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


store = SessionStore(SESSION_TTL_SECONDS, MAX_HISTORY_TURNS)

app = FastAPI(title="Survey Builder LLM App", version="2.0.0")


# --- LLM helpers ---
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _schema(name: str, extra_props: Dict[str, Any]) -> Dict[str, Any]:
    props: Dict[str, Any] = {
        "reply": {"type": "string"},
        "operations": {"type": "array", "items": {"type": "object"}},
        # Optional quick-reply suggestions for the turn's "reply" - short, clickable
        # answers a user could send as-is. Omitted or empty when there's nothing
        # sensible to suggest (e.g. a turn that just built something).
        "chips": {"type": "array", "items": {"type": "string"}},
    }
    props.update(extra_props)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": {
                "type": "object",
                "properties": props,
                "required": ["reply", "operations"],
            },
        },
    }


CHAT_SCHEMA = _schema("chat_response", {})
GENERATE_SCHEMA = _schema("survey_plan", {"plan": {"type": "object"}})


def _response_format(schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if RESPONSE_FORMAT == "json_schema":
        return schema
    if RESPONSE_FORMAT == "json_object":
        return {"type": "json_object"}
    return None


def _complete(messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]] = None) -> str:
    kwargs: Dict[str, Any] = {
        "model": SERVING_ENDPOINT,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
    }
    # GPT-5 reasoning models reject a custom temperature.
    if "gpt-5" not in SERVING_ENDPOINT:
        kwargs["temperature"] = TEMPERATURE
    if schema is not None:
        rf = _response_format(schema)
        if rf is not None:
            kwargs["response_format"] = rf
    try:
        completion = _get_client().chat.completions.create(**kwargs)
    except Exception as exc:  # surface upstream errors to the caller
        raise HTTPException(status_code=502, detail=f"Model request failed: {exc}") from exc
    return completion.choices[0].message.content or ""


MAX_CHIPS = 4


def _clean_chips(raw: Any) -> List[str]:
    """Coerce the model's chips into a short list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    cleaned = [str(item).strip() for item in raw if isinstance(item, (str, int, float))]
    return [item for item in cleaned if item][:MAX_CHIPS]


def _parse_json(text: str) -> Dict[str, Any]:
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Lenient fallback: isolate the outermost object and drop trailing commas,
    # the two most common defects in model-generated JSON.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1]
        candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)
        return json.loads(candidate)
    return json.loads(cleaned)


def _complete_json(
    messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], str]:
    """Call the model and parse JSON, retrying once with the repair prompt."""
    raw = _complete(messages, schema)
    try:
        return _parse_json(raw), raw
    except json.JSONDecodeError:
        pass

    repair_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": prompts.REPAIR},
    ]
    raw2 = _complete(repair_messages, schema)
    try:
        return _parse_json(raw2), raw2
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Model did not return valid JSON after repair: {exc}",
        ) from exc


def _run(
    mode: str,
    messages: List[Dict[str, str]],
    schema: Dict[str, Any],
    tags: Dict[str, str],
) -> Tuple[Dict[str, Any], str]:
    """Run a completion, wrapping it in an MLflow trace span when enabled."""
    if not _TRACING:
        return _complete_json(messages, schema)
    try:
        with mlflow.start_span(name=mode) as span:
            span.set_inputs({"messages": messages})
            parsed, raw = _complete_json(messages, schema)
            try:
                span.set_outputs(parsed)
                mlflow.update_current_trace(tags=tags)
            except Exception:  # noqa: BLE001
                pass
            return parsed, raw
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - tracing must not break the request
        return _complete_json(messages, schema)



# --- Request/response models ---
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    survey_json: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    operations: List[Dict[str, Any]]
    chips: List[str] = []


class GenerateRequest(BaseModel):
    description: str = Field(..., min_length=1)
    survey_json: Optional[str] = None
    session_id: Optional[str] = None


class GenerateResponse(BaseModel):
    session_id: str
    plan: Optional[Dict[str, Any]] = None
    reply: str
    operations: List[Dict[str, Any]]
    chips: List[str] = []


class ResetRequest(BaseModel):
    session_id: str


# --- Endpoints ---
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "endpoint": SERVING_ENDPOINT}


@app.get("/")
def root() -> Dict[str, object]:
    return {
        "app": "Survey Builder LLM App",
        "endpoint": SERVING_ENDPOINT,
        "endpoints": {
            "chat": "POST /chat {\"message\": \"...\", \"survey_json\": \"optional\", \"session_id\": \"optional\"}",
            "generate": "POST /generate {\"description\": \"...\", \"survey_json\": \"optional\"}",
            "reset": "POST /reset {\"session_id\": \"...\"}",
        },
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session_id = req.session_id or str(uuid.uuid4())
    survey_json = req.survey_json if req.survey_json is not None else DEFAULT_SURVEY_JSON

    user_turn = prompts.CHAT_USER_TURN.replace(
        "«CURRENT SURVEY (JSON)»", survey_json
    ).replace("«the user's request»", req.message)

    history = store.get_history(session_id)
    messages = [{"role": "system", "content": prompts.CHAT_SYSTEM}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_turn})

    parsed, raw = _run(
        "chat",
        messages,
        CHAT_SCHEMA,
        {"mode": "chat", "session_id": session_id, "endpoint": SERVING_ENDPOINT},
    )
    store.append(session_id, user_turn, raw)

    return ChatResponse(
        session_id=session_id,
        reply=str(parsed.get("reply", "")),
        operations=parsed.get("operations", []) or [],
        chips=_clean_chips(parsed.get("chips")),
    )


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    session_id = req.session_id or str(uuid.uuid4())
    survey_json = req.survey_json if req.survey_json is not None else DEFAULT_SURVEY_JSON

    user_turn = prompts.GENERATE_USER_TURN.replace(
        "«CURRENT SURVEY (JSON, one empty page)»", survey_json
    ).replace("«the survey description»", req.description)

    history = store.get_history(session_id)
    messages = [{"role": "system", "content": prompts.GENERATE_SYSTEM}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_turn})

    parsed, raw = _run(
        "generate",
        messages,
        GENERATE_SCHEMA,
        {"mode": "generate", "session_id": session_id, "endpoint": SERVING_ENDPOINT},
    )
    store.append(session_id, user_turn, raw)

    return GenerateResponse(
        session_id=session_id,
        plan=parsed.get("plan"),
        reply=str(parsed.get("reply", "")),
        operations=parsed.get("operations", []) or [],
        chips=_clean_chips(parsed.get("chips")),
    )


@app.post("/reset")
def reset(req: ResetRequest) -> Dict[str, object]:
    existed = store.reset(req.session_id)
    return {"session_id": req.session_id, "cleared": existed}
