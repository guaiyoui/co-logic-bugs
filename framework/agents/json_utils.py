"""LLM JSON extraction helpers shared by the hunter and triager."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

LOGGER = logging.getLogger(__name__)

_FENCED = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Extract a JSON object or array from an LLM response.

    Prefers a fenced ```json block; otherwise takes the outermost bracket
    span of whichever JSON type appears first.
    """
    if not text:
        raise ValueError("empty LLM response")
    match = _FENCED.search(text)
    payload = match.group(1) if match else text
    obj_start = payload.find("{")
    arr_start = payload.find("[")
    if obj_start < 0 and arr_start < 0:
        raise ValueError("no JSON payload found in LLM response")
    if arr_start >= 0 and (obj_start < 0 or arr_start < obj_start):
        end = payload.rfind("]")
        if end <= arr_start:
            raise ValueError("unterminated JSON array in LLM response")
        return json.loads(payload[arr_start : end + 1])
    end = payload.rfind("}")
    if end <= obj_start:
        raise ValueError("unterminated JSON object in LLM response")
    return json.loads(payload[obj_start : end + 1])


def call_llm_json(
    agent: Any,
    prompt: str,
    temperature: float = 0.7,
    retries: int = 1,
    event: str = "generic",
    max_tokens: int = 2000,
) -> Any:
    """Call an agent's LLM expecting JSON; retry once on parse failure.

    The retry appends a stricter formatting reminder. Returns the parsed
    payload or raises ValueError after all attempts fail.
    """
    last_error: Exception | None = None
    attempt_prompt = prompt
    for attempt in range(retries + 1):
        response = agent.call_llm(
            attempt_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            event=event if attempt == 0 else f"{event}:retry",
        )
        if response is None:
            last_error = ValueError("LLM call returned no response")
            continue
        try:
            return extract_json(response)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            LOGGER.warning("LLM JSON parse failed (attempt %s): %s", attempt + 1, exc)
            attempt_prompt = (
                prompt
                + "\n\nIMPORTANT: your previous reply was not parseable. "
                "Output ONLY a single ```json fenced block and nothing else."
            )
    raise ValueError(f"LLM JSON extraction failed: {last_error}")
