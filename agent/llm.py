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
    pass


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
        self.transport = transport                      # tests inject a fake: (payload) -> response dict
        self.enabled = bool(enabled and (self.key or transport))

    # ---- low level ---------------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        if self.transport:
            return self.transport(payload)
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        last = None
        for attempt in range(4):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}"
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    # Do not echo the body into logs/reports: providers may include account identifiers.
                    raise LLMUnavailable(f"LLM request failed with HTTP {r.status_code}")
                return r.json()
            except requests.RequestException as exc:
                last = type(exc).__name__
                time.sleep(2 ** attempt)
        raise LLMUnavailable(f"LLM unreachable after retries ({last})")

    def chat(self, step: str, messages: list[dict], tools: dict[str, Tool] | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": self.max_out}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": n, "description": s.get("description", ""),
                                                                    "parameters": {k: v for k, v in s.items() if k != "description"}}}
                                for n, (s, _fn) in tools.items()]
            payload["tool_choice"] = "auto"
        data = self._post(payload)
        usage = data.get("usage") or {}
        self.ctx.add_tokens(step, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        return data["choices"][0]["message"]

    # ---- agent loop --------------------------------------------------------------------------
    def run_agent(self, step: str, system: str, user: str, tools: dict[str, Tool],
                  on_tool: Callable[[str, dict, object], None] | None = None) -> str:
        """Let the model call `tools` until it answers in plain text (or the turn budget is spent)."""
        if not self.enabled:
            raise LLMUnavailable("LLM disabled (no API key or --no-llm)")
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _turn in range(self.max_turns):
            msg = self.chat(step, messages, tools)
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
            if not calls:
                return (msg.get("content") or "").strip()
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
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": text[:6000]})
        # budget exhausted: ask for a final answer without tools
        messages.append({"role": "user", "content": "Tool budget exhausted. Give your final answer now."})
        return (self.chat(step, messages).get("content") or "").strip()


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
            if part[:1] in "{[":
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
