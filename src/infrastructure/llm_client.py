"""Groq LLM client wrapper.

Groq's API is OpenAI-compatible, so tool calling uses the standard
`tools` / `tool_choice` / `tool_calls` protocol. The agentic loop runs
until the model stops issuing tool_calls or the budget is hit.

Free tier limits: 30 RPM, 14 400 RPD, 6 000 TPM — the rate-limit handler
backs off when hit, which is expected during multi-flag investigations.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Callable

from src.config import LLMConfig


class LLMNotConfiguredError(RuntimeError):
    pass


class LLMAuthenticationError(RuntimeError):
    """Raised when the Groq API rejects the key."""
    pass


class GroqClient:
    def __init__(self, config: LLMConfig, api_key: str | None = None):
        self.config = config
        self._api_key = api_key or config.api_key
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self):
        if not self._api_key:
            raise LLMNotConfiguredError(
                "GROQ_API_KEY is not set. The deterministic scanner still "
                "works; LLM investigation and narration require a free key "
                "from console.groq.com.")
        if self._client is None:
            from groq import Groq
            self._client = Groq(api_key=self._api_key)
        return self._client

    # ---- malformed-tool-call recovery -----------------------------------
    @staticmethod
    def _parse_fallback_tool_calls(exc_str: str) -> list[tuple[str, dict]]:
        """Groq sometimes generates <function=name,{json}></function> instead of
        the standard function-calling format and returns a 400 tool_use_failed.
        Also handles truncated calls where the JSON is cut off mid-generation."""
        calls = []

        # Pass 1: complete calls with closing </function> tag
        # Groq uses several separators between name and JSON: comma, equals, paren, or >
        for m in re.finditer(r'<function=(\w+)[,=(>]\(?(\{.*?\})\)?\s*</function>',
                              exc_str, re.DOTALL):
            try:
                calls.append((m.group(1), json.loads(m.group(2))))
            except json.JSONDecodeError:
                pass

        if calls:
            return calls

        # Pass 2: truncated calls — no closing tag, JSON may be cut off mid-stream.
        # Extract everything from the opening { to end of string and try to close it.
        for m in re.finditer(r'<function=(\w+)[,=(>]\(?(\{[^<]*)', exc_str, re.DOTALL):
            fn_name = m.group(1)
            partial = m.group(2).strip()
            # Remove trailing incomplete token (e.g. trailing comma or partial key)
            fixed = re.sub(r',\s*$', '', partial)
            if not fixed.endswith('}'):
                fixed += '}'
            try:
                calls.append((fn_name, json.loads(fixed)))
            except json.JSONDecodeError:
                pass

        return calls

    # ---- helpers for rate-limit back-off (Groq free tier is 30 RPM) --------
    @staticmethod
    def _backoff_create(client, **kwargs):
        """Call chat.completions.create with one retry on rate-limit (429)
        and a clear message on authentication failure (401)."""
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            exc_str = str(exc).lower()
            # Authentication failure — don't retry, surface clearly
            if "401" in str(exc) or "invalid_api_key" in exc_str or "authentication" in exc_str:
                raise LLMAuthenticationError(
                    "Groq API key is invalid. Please check your key at "
                    "console.groq.com/keys and paste it again."
                ) from exc
            # Rate limit — wait and retry once
            if "429" in str(exc) or "rate_limit" in exc_str:
                time.sleep(5)
                return client.chat.completions.create(**kwargs)
            raise

    # ------------------------------------------------------------------ calls
    def complete(self, system: str, user: str, model: str | None = None,
                 max_tokens: int | None = None) -> str:
        """Simple completion — no tools."""
        client = self._ensure_client()
        resp = self._backoff_create(
            client,
            model=model or self.config.narrator_model,
            max_tokens=max_tokens or self.config.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    def run_tool_loop(self, system: str, user: str, tools: list[dict],
                      dispatcher: Callable[[str, dict], dict],
                      model: str | None = None,
                      on_event: Callable[[dict], None] | None = None,
                      ) -> tuple[str, list[dict]]:
        """Standard agentic loop: model <-> tools until stop or budget hit.

        Returns (final_text, trace).
        """
        client = self._ensure_client()
        model = model or self.config.investigator_model
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        trace: list[dict] = []

        for _ in range(self.config.max_tool_iterations):
            try:
                resp = self._backoff_create(
                    client,
                    model=model,
                    max_tokens=self.config.max_tokens,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                msg = resp.choices[0].message
                text = msg.content or ""
                tool_calls = msg.tool_calls or []
            except Exception as exc:
                # Groq rejects its own <function=name,{json}> fallback format.
                # Recover by parsing and executing those calls ourselves.
                if "tool_use_failed" not in str(exc):
                    raise
                recovered = self._parse_fallback_tool_calls(str(exc))
                if not recovered:
                    raise
                synthetic = [
                    {"id": f"rec_{uuid.uuid4().hex[:8]}", "type": "function",
                     "function": {"name": fn, "arguments": json.dumps(args)}}
                    for fn, args in recovered
                ]
                messages.append({"role": "assistant", "content": None,
                                  "tool_calls": synthetic})
                for stub, (fn_name, args) in zip(synthetic, recovered):
                    try:
                        output = dispatcher(fn_name, args)
                    except Exception as de:
                        output = {"error": str(de)}
                    event = {"tool": fn_name, "input": args, "output": output}
                    trace.append(event)
                    if on_event:
                        on_event(event)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": stub["id"],
                        "content": json.dumps(output, default=str)[:4000],
                    })
                continue  # resume the loop with the recovered results

            if not tool_calls:
                return text, trace

            # Append assistant message with tool_calls to history.
            # Groq returns 'annotations' in responses but rejects it in requests.
            _UNSUPPORTED = {"annotations"}
            messages.append({k: v for k, v in msg.model_dump().items()
                              if k not in _UNSUPPORTED and v is not None})

            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                try:
                    output = dispatcher(fn_name, args)
                except Exception as exc:
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                event = {"tool": fn_name, "input": args, "output": output}
                trace.append(event)
                if on_event:
                    on_event(event)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(output, default=str)[:4000],
                })

        # Budget exhausted: ask for a final answer without tools.
        messages.append({"role": "user",
                         "content": "Tool budget reached. Provide your final structured answer now."})
        resp = self._backoff_create(
            client,
            model=model,
            max_tokens=self.config.max_tokens,
            messages=messages,
        )
        return resp.choices[0].message.content or "", trace
