"""Thin wrapper around Gemini. The rest of the agent only sees Turn objects, so swapping the
model (or using a scripted fake in tests) means changing only this file."""
import os
import time
from dataclasses import dataclass, field

DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"  # 2.5-flash free tier is only 20 requests/day


@dataclass
class Turn:
    text: str = ""
    calls: list = field(default_factory=list)   # [{"name": ..., "args": {...}}]
    tokens_in: int = 0
    tokens_out: int = 0


def _clean_schema(s):
    """MCP gives JSON-schema; Gemini wants its own simpler subset."""
    if "anyOf" in s:  # Optional[x] shows up as anyOf [x, null]
        s = next((x for x in s["anyOf"] if x.get("type") != "null"), {"type": "string"})
    kind = s.get("type", "string")
    out = {"type": kind.upper()}
    if s.get("description"):
        out["description"] = s["description"]
    if kind == "object":
        out["properties"] = {k: _clean_schema(v) for k, v in s.get("properties", {}).items()}
        if s.get("required"):
            out["required"] = s["required"]
    if kind == "array":
        out["items"] = _clean_schema(s.get("items", {"type": "string"}))
    return out


def declarations_from_mcp(tools, hidden=()):
    return [{"name": t.name, "description": (t.description or "").strip(),
             "parameters": _clean_schema(t.inputSchema)}
            for t in tools if t.name not in hidden]


def api_keys():
    """GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3 ... (free tier is capped per day per key)."""
    keys = [os.environ.get("GEMINI_API_KEY")] + [os.environ.get(f"GEMINI_API_KEY_{i}") for i in range(2, 10)]
    return [k for k in keys if k]


class _Keys:
    """Shared across all chats in a run, so a key that is used up stays skipped."""
    dead = set()


class GeminiChat:
    def __init__(self, system, decls, model=None):
        from google import genai
        from google.genai import types
        self.types = types
        self.genai = genai
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.keys = api_keys()
        self.key_i = next((i for i, k in enumerate(self.keys) if k not in _Keys.dead), 0)
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            temperature=0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self._open_chat(history=None)

    def _open_chat(self, history):
        # timeout so a dropped connection can never hang the run forever
        self.client = self.genai.Client(api_key=self.keys[self.key_i],
                                        http_options=self.types.HttpOptions(timeout=90_000))
        self.chat = self.client.chats.create(model=self.model, config=self.config, history=history)

    def _next_key(self):
        """Current key is used up or invalid: move to the next live one, keeping the conversation."""
        _Keys.dead.add(self.keys[self.key_i])
        live = [i for i, k in enumerate(self.keys) if k not in _Keys.dead]
        if not live:
            return False
        history = self.chat.get_history()
        self.key_i = live[0]
        self._open_chat(history)
        return True

    def send(self, text):
        return self._send(text)

    def send_results(self, results):
        """results: [(tool_name, result_dict), ...]"""
        parts = [self.types.Part.from_function_response(name=n, response={"result": r}) for n, r in results]
        return self._send(parts)

    def _send(self, message):
        waits = [5, 15, 30, 60, 60, 60]  # per-minute limits clear in under a minute
        while True:
            try:
                resp = self.chat.send_message(message)
                break
            except Exception as e:
                err = str(e)
                if "PerDay" in err or "API_KEY_INVALID" in err:
                    if self._next_key():
                        continue
                    raise
                transient = any(k in err or k in type(e).__name__ for k in (
                    "429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "Connection", "ReadError",
                    "ConnectError", "Timeout", "RemoteProtocol", "ServerError"))
                if not transient or not waits:
                    raise
                time.sleep(waits.pop(0))

        turn = Turn()
        usage = resp.usage_metadata
        if usage:
            turn.tokens_in = usage.prompt_token_count or 0
            turn.tokens_out = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        cand = resp.candidates[0] if resp.candidates else None
        if cand and cand.content and cand.content.parts:
            for p in cand.content.parts:
                if getattr(p, "function_call", None):
                    turn.calls.append({"name": p.function_call.name, "args": dict(p.function_call.args or {})})
                elif getattr(p, "text", None):
                    turn.text += p.text
        return turn
