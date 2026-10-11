"""Tiny OpenAI-compatible chat client with a tool-calling agent loop and token accounting.

No SDK: plain `requests`, so it works with Groq, NVIDIA NIM, OpenAI, Ollama, ... Anything that speaks
POST {base_url}/chat/completions with `tools` support.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable

import requests

# A tool is (json-schema "parameters" dict, python callable taking **kwargs and returning a JSON-able value)
Tool = tuple[dict, Callable[..., object]]


class LLMUnavailable(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code                                  # provider's short error code (never the body), e.g. "tool_use_failed"


class LLM:
    def __init__(self, cfg: dict, ctx, enabled: bool = True, transport: Callable | None = None):
        c = cfg["llm"]
        self.base_url = os.environ.get("LLM_BASE_URL", c["base_url"]).rstrip("/")
        self.model = os.environ.get("LLM_MODEL", c["model"])
        self.key = os.environ.get(c["api_key_env"], "") or os.environ.get("LLM_API_KEY", "")
        self.max_turns = int(c.get("max_tool_turns", 8))
        self.max_out = int(c.get("max_output_tokens", 1500))
        self.timeout = int(c.get("timeout_s", 90))
        self.ctx = ctx
        self.last_text = ""                             # final reply of the last agent run / why it failed (for the report trace)
        self.last_error = ""
        self.transport = transport                      # tests inject a fake: (payload) -> response dict
        self.enabled = bool(enabled and (self.key or transport))

    # ---- low level ---------------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        if self.transport:
            return self.transport(payload)
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        last = None
        for attempt in range(6):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}"
                    time.sleep(self._backoff(r, attempt))
                    continue
                if r.status_code >= 400:
                    # Do not echo the body into logs/reports: providers may include account identifiers. The short error
                    # code is safe and tells "malformed tool call" (recoverable) from "bad request" (not).
                    code = ""
                    try:
                        err = (r.json() or {}).get("error") or {}
                        code = str(err.get("code") or err.get("type") or "")[:40] if isinstance(err, dict) else ""
                    except ValueError:
                        pass
                    raise LLMUnavailable(f"LLM request failed with HTTP {r.status_code}" + (f" ({code})" if code else ""), code)
                return r.json()
            except requests.RequestException as exc:
                last = type(exc).__name__
                time.sleep(2 ** attempt)
        raise LLMUnavailable(f"LLM unreachable after retries ({last})")

    @staticmethod
    def _backoff(resp, attempt: int) -> float:
        """Seconds to wait before retrying. A rate limit (429) names its own wait in `Retry-After`; free tiers cap tokens per
        minute, so waiting as told (up to 45 s) beats hammering the API with 1-2 s retries."""
        try:
            hinted = float(resp.headers.get("retry-after", ""))
        except (TypeError, ValueError):
            hinted = 0.0
        if resp.status_code == 429:
            return min(max(hinted, 5.0 * (attempt + 1)), 45.0)
        return min(max(hinted, 2.0 ** attempt), 20.0)

    def chat(self, step: str, messages: list[dict], tools: dict[str, Tool] | None = None, max_tokens: int | None = None,
             tool_choice: str = "auto") -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens or self.max_out}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": n, "description": s.get("description", ""),
                                                                    "parameters": {k: v for k, v in s.items() if k != "description"}}}
                                for n, (s, _fn) in tools.items()]
            payload["tool_choice"] = tool_choice
        data = self._post(payload)
        usage = data.get("usage") or {}
        self.ctx.add_tokens(step, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return data["choices"][0]["message"]

    # ---- agent loop --------------------------------------------------------------------------
    def run_agent(self, step: str, system: str, user: str, tools: dict[str, Tool],
                  on_tool: Callable[[str, dict, object], None] | None = None, until: Callable[[], bool] | None = None,
                  max_turns: int | None = None, max_tokens: int | None = None,
                  on_text: Callable[[str], str | None] | None = None) -> str:
        """Let the model call `tools` until it answers in plain text (or the turn budget is spent).

        `until` verifies the work: models sometimes announce a fix without ever calling the tool that applies it. While
        `until()` is False a plain-text answer is not accepted; the model is told to use the tools (at most twice).
        `on_text(reply)` may turn a plain-text reply into feedback for the next turn (e.g. a script pasted in a code block
        instead of a tool call); return None to treat the reply as the final answer."""
        if not self.enabled:
            raise LLMUnavailable("LLM disabled (no API key or --no-llm)")
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        nudges = recoveries = 0
        for _turn in range(max_turns or self.max_turns):
            try:
                msg = self.chat(step, messages, tools, max_tokens)
            except LLMUnavailable as exc:
                if exc.code == "tool_use_failed" and recoveries < 3:        # the model produced a malformed tool call: tell it, retry
                    recoveries += 1
                    messages.append({"role": "user", "content": "Your last tool call was malformed and could not be parsed. Send it again with "
                                                                "valid JSON arguments (escape quotes and newlines inside strings), or reply "
                                                                "with the complete script in one ```python block."})
                    continue
                raise
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
            if not calls:
                feedback = on_text((msg.get("content") or "")) if on_text else None
                if feedback:
                    messages.append({"role": "user", "content": feedback})
                    continue
                if until is not None and not until() and nudges < 2:
                    nudges += 1
                    messages.append({"role": "user", "content": "The task is not complete: the tools have not accepted a result yet. "
                                                                "Do not just describe a change - call the tool now."})
                    continue
                self.last_text = (msg.get("content") or "").strip()
                return self.last_text
            for call in calls:
                fn = call["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if name not in tools:
                        result: object = {"error": f"unknown tool {name}"}
                    else:
                        result = tools[name][1](**args)
                except Exception as exc:  # noqa: BLE001 - tool errors go back to the model, not up the stack
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                if on_tool:
                    on_tool(name, args if isinstance(args, dict) else {}, result)
                text = json.dumps(result, default=str)
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": text[:4000]})          # every turn resends the history: keep tool results small (tokens-per-minute limits)
        # budget exhausted: ask for a final answer without tools
        messages.append({"role": "user", "content": "Tool budget exhausted. Give your final answer now."})
        # tools stay defined (the history contains tool calls, which providers reject without them) but cannot be used now
        self.last_text = (self.chat(step, messages, tools, max_tokens, tool_choice="none").get("content") or "").strip()
        return self.last_text


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences and chatter)."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        for part in t.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part[:1] and part[:1] in "{[":              # ("" in "{[" is True: skip the empty piece before the first fence)
                t = part
                break
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None
