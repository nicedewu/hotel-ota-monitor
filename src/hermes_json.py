from __future__ import annotations

from typing import Literal


HermesOutputWrapper = Literal["bare", "markdown_fence"]


def normalize_hermes_json_output(raw_output: str) -> tuple[str, HermesOutputWrapper]:
    """Return a strict JSON payload after removing one complete JSON code fence.

    Hermes occasionally wraps an otherwise valid response in a Markdown fence.
    Only a fence enclosing the entire response is removed; prefixes, suffixes and
    other fence languages intentionally remain invalid JSON for the caller.
    """
    output = str(raw_output or "").strip()
    lines = output.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip(), "markdown_fence"
    return output, "bare"
