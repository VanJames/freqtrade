#!/usr/bin/env python3
"""
Generate a market-regime recommendation using the OpenAI Responses API.

This is intentionally read-only: it does not alter trading parameters or
touch the live bot. It only returns a suggested profile and rationale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def infer_base_url(model: str, base_url: str | None = None) -> str:
    if base_url and base_url.strip():
        return base_url.strip()
    model_name = model.lower().strip()
    if model_name.startswith("deepseek"):
        return "https://api.deepseek.com"
    return "https://api.openai.com/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask OpenAI for a FreqAI profile recommendation.")
    parser.add_argument("--snapshot", help="Path to a JSON market snapshot. Defaults to stdin.")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-5.5"),
        help="OpenAI model to use. Defaults to OPENAI_MODEL or gpt-5.5.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", ""),
        help="OpenAI API base URL.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--output", help="Optional output path for the JSON response.")
    return parser.parse_args()


def load_snapshot(path: str | None) -> dict[str, Any]:
    if path:
        return json.loads(Path(path).read_text())
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)


def build_prompt(snapshot: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": "Recommend one of conservative, balanced, aggressive FreqAI profiles.",
            "constraints": [
                "Return strict JSON only.",
                "Do not suggest changing live trading code.",
                "Prefer stable profile selection over overfitting.",
            ],
            "profile_schema": {
                "profile": "conservative|balanced|aggressive",
                "confidence": "number from 0 to 1",
                "reason": "short explanation",
                "suggested_changes": ["optional short bullet strings"],
            },
            "snapshot": snapshot,
        },
        ensure_ascii=False,
    )


def extract_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()

    chunks: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


def resolve_endpoint(base_url: str) -> tuple[str, str]:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return normalized, "chat_completions"
    if path.endswith("/responses"):
        return normalized, "responses"
    if "openrouter.ai" in parsed.netloc:
        return normalized + "/chat/completions", "chat_completions"
    if "deepseek.com" in parsed.netloc:
        return normalized + "/chat/completions", "chat_completions"
    return normalized + "/responses", "responses"


def call_openai(*, base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> dict[str, Any]:
    url, mode = resolve_endpoint(base_url)
    if mode == "chat_completions":
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a quantitative trading assistant. "
                            "Return valid JSON only, with keys profile, confidence, reason, suggested_changes."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            }
        ).encode("utf-8")
    else:
        body = json.dumps(
            {
                "model": model,
                "input": prompt,
                "instructions": (
                    "You are a quantitative trading assistant. "
                    "Return valid JSON only, with keys profile, confidence, reason, suggested_changes."
                ),
            }
        ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    output_text = extract_output_text(payload)
    if output_text:
        try:
            return json.loads(output_text)
        except json.JSONDecodeError:
            return {"raw_output": output_text, "response": payload}

    choices = payload.get("choices", []) or []
    if choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str) and content.strip():
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"raw_output": content, "response": payload}
    return payload


def main() -> int:
    args = parse_args()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set.")

    snapshot = load_snapshot(args.snapshot)
    prompt = build_prompt(snapshot)
    base_url = infer_base_url(args.model, args.base_url)
    payload = call_openai(
        base_url=base_url,
        api_key=api_key,
        model=args.model,
        prompt=prompt,
        timeout=args.timeout,
    )
    result = normalize_payload(payload)

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
