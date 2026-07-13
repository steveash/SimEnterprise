#!/usr/bin/env python
"""Cred-gated Amazon Bedrock live smoke (spec 0001, slice 7).

Unlike ``scripts/import_smoke.py`` (which only *constructs* the backend, no
network, and runs in CI), this script makes two **real** Bedrock calls — one
``generate_structured`` and one ``generate_content`` — to prove the end-to-end
path works against a live account. It is therefore **never run by CI**: it is a
manual validation you invoke once you have AWS credentials:

    uv sync --extra bench && uv run python scripts/bedrock_smoke.py

It is safe to run anywhere. With no AWS credential signal in the environment
(``AWS_ACCESS_KEY_ID`` / ``AWS_PROFILE`` / ``AWS_BEARER_TOKEN_BEDROCK`` /
``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI``), it prints a skip line and exits 0
without touching the network.

Model id: Bedrock addresses models by dated inference-profile id, whose exact
form varies by account and region, so it is taken from the ``BEDROCK_SMOKE_MODEL``
env var, defaulting to ``us.anthropic.claude-sonnet-4-6-20250929-v1:0``. A 1P-form
id (``claude-sonnet-4-6``) is rejected at client build by the repo's fail-fast
guard (finding F2) with the inference-profile shape to set — that check firing is
the intended behavior, not a bug. Region comes from ``AWS_REGION`` when set,
otherwise the ambient AWS default.
"""

from __future__ import annotations

import os

_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6-20250929-v1:0"

# Any one of these signals AWS credentials are configured; absent all of them the
# smoke skips rather than raising, so it stays safe to run in CI or a bare shell.
_AWS_CRED_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
)


def main() -> None:
    if not any(os.environ.get(name) for name in _AWS_CRED_ENV):
        print("bedrock live smoke: skipped: no AWS credentials detected")
        return

    from enterprise_sim.core.llm import (
        LLMConfig,
        assemble_prompt,
        build_client,
    )

    model = os.environ.get("BEDROCK_SMOKE_MODEL", _DEFAULT_MODEL)
    # A 1P-form model id fails fast here (finding F2), before any live call.
    client = build_client(
        LLMConfig(
            backend="bedrock",
            model=model,
            aws_region=os.environ.get("AWS_REGION"),
        )
    )

    structured = client.generate_structured(
        assemble_prompt(
            system="Return the requested fields as JSON.",
            brief="The sky on a clear day: give its color and a 0-1 confidence.",
        ),
        schema={
            "type": "object",
            "properties": {
                "color": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["color", "confidence"],
        },
    )
    print(
        f"generate_structured: model={structured.model} data={structured.data} "
        f"usage={structured.usage.to_dict()}"
    )

    content = client.generate_content(
        assemble_prompt(
            system="You write one-sentence status notes.",
            brief="Write a one-sentence note that the Bedrock smoke test passed.",
        ),
        candidate_references=("artifact-0",),
    )
    print(
        f"generate_content: model={content.model} usage={content.usage.to_dict()} "
        f"text={content.content!r}"
    )

    print(f"bedrock live smoke: OK (total spend ${client.cost.total_cost_usd:.4f})")


if __name__ == "__main__":
    main()
