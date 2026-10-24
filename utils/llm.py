"""
LLM client shared by the AI chat cog and the dashboard AI agent.

Provider order:
  1. Groq        (GROQ_API_KEY)        - every model in GROQ_MODELS, in order
  2. OpenRouter  (OPENROUTER_API_KEY)  - FREE models only (":free" or openrouter/free)

Both speak the OpenAI-compatible /chat/completions API, so one request path
serves both. A provider with no key is skipped. Calls are blocking (requests) -
run them in an executor from async code.
"""
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODELS = [
    m.strip() for m in os.getenv(
        "GROQ_MODELS", "openai/gpt-oss-120b,llama-3.3-70b-versatile,openai/gpt-oss-20b"
    ).split(",") if m.strip()
]
GROQ_REASONING_EFFORT = os.getenv("GROQ_REASONING_EFFORT", "low").strip()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
_OR_RAW = [m.strip() for m in os.getenv("OPENROUTER_MODELS", "openrouter/free").split(",") if m.strip()]


def _is_free(model: str) -> bool:
    return model == "openrouter/free" or model.endswith(":free")


# Safety net: a paid model in the env var is ignored, never used.
OPENROUTER_MODELS = [m for m in _OR_RAW if _is_free(m)] or ["openrouter/free"]
if any(not _is_free(m) for m in _OR_RAW):
    logger.warning("OPENROUTER_MODELS: ignoring non-free model(s); only ':free' or 'openrouter/free' allowed")

TIMEOUT = float(os.getenv("AGENT_LLM_TIMEOUT", "25"))
_session = requests.Session()


def providers() -> list:
    return [
        {"name": "groq", "url": "https://api.groq.com/openai/v1/chat/completions",
         "key": GROQ_API_KEY, "models": GROQ_MODELS},
        {"name": "openrouter", "url": "https://openrouter.ai/api/v1/chat/completions",
         "key": OPENROUTER_API_KEY, "models": OPENROUTER_MODELS},
    ]


def is_configured() -> bool:
    return bool(GROQ_API_KEY or OPENROUTER_API_KEY)


def _post(provider, model, messages, tools, temperature, max_tokens):
    body = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if max_tokens:
        body["max_tokens"] = max_tokens
    if provider["name"] == "groq" and "gpt-oss" in model and GROQ_REASONING_EFFORT:
        body["reasoning_effort"] = GROQ_REASONING_EFFORT
    headers = {"Authorization": f"Bearer {provider['key']}", "Content-Type": "application/json"}
    if provider["name"] == "openrouter":
        headers["X-Title"] = "Discord Helper Bot"

    for attempt in range(2):
        resp = _session.post(provider["url"], headers=headers, json=body, timeout=TIMEOUT)
        if resp.status_code == 400 and "reasoning_effort" in body:
            body.pop("reasoning_effort")  # model doesn't take it - retry without
            continue
        if resp.status_code == 400 and attempt == 0 and (
                "tool_use_failed" in resp.text or "failed_generation" in resp.text):
            continue  # model produced a malformed tool call - one retry on the same model
        if resp.status_code == 429:
            break  # rate limited: fail over to the next model/provider right away
        if resp.status_code in (500, 502, 503, 504) and attempt == 0:
            time.sleep(1)
            continue
        break
    if resp.status_code != 200:
        raise RuntimeError(f"{provider['name']}/{model} -> HTTP {resp.status_code}: {resp.text[:200]}")
    msg = resp.json()["choices"][0]["message"]
    return msg


def chat(messages: list, tools: list = None, temperature: float = 0.3, max_tokens: int = None) -> dict:
    """One completion. Returns the assistant message dict (may contain tool_calls)."""
    if not is_configured():
        raise RuntimeError("No LLM configured (set GROQ_API_KEY and/or OPENROUTER_API_KEY).")
    last = None
    for sweep in range(2):
        for p in providers():
            if not p["key"]:
                continue
            for model in p["models"]:
                try:
                    return _post(p, model, messages, tools, temperature, max_tokens)
                except Exception as e:  # noqa: BLE001 - any failure -> next model
                    last = e
                    logger.warning("LLM attempt failed: %s", e)
        # Every model failed. If it was rate limiting (common on free tiers when a task
        # makes several calls in a row), wait a few seconds and sweep once more.
        if sweep == 0 and last is not None and "429" in str(last):
            time.sleep(15)
            continue
        break
    raise RuntimeError(f"All LLM providers failed. Last error: {last}")


def simple_chat(messages: list, temperature: float = 0.7, max_tokens: int = 600) -> str:
    return (chat(messages, None, temperature, max_tokens).get("content") or "").strip()


# --------------------------------------------------------------------------
# Optional web search (Tavily)
# --------------------------------------------------------------------------
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current info or facts you're unsure about.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def web_search(query: str, max_results: int = 5) -> dict:
    if not TAVILY_API_KEY:
        return {"error": "Web search isn't configured (TAVILY_API_KEY missing)."}
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_API_KEY, "query": query, "max_results": max_results},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        return {"results": [
            {"title": x.get("title"), "url": x.get("url"), "content": (x.get("content") or "")[:500]}
            for x in data.get("results", [])
        ]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Search failed: {e}"}


def run_tool_loop(messages: list, tools: list, execute, max_iterations: int = 8,
                  temperature: float = 0.3, max_tokens: int = None) -> str:
    """Generic tool loop. `execute(name, args) -> dict` runs one tool.
    Returns the final assistant text."""
    msgs = list(messages)
    for _ in range(max_iterations):
        msg = chat(msgs, tools, temperature, max_tokens)
        calls = msg.get("tool_calls")
        if not calls:
            return (msg.get("content") or "").strip()
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = execute(name, args)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            msgs.append({"role": "tool", "tool_call_id": c["id"], "name": name,
                         "content": json.dumps(result, default=str)[:6000]})
    return "I hit my tool-call limit before finishing - try asking in smaller steps."
