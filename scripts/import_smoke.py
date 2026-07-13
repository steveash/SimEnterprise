#!/usr/bin/env python
"""Real-LLM runtime import smoke (esim-sr3).

The deterministic test suite runs keyless on the `fake` backend, so it never
loads the *runtime* dependencies of the real-LLM paths — the `anthropic` SDK
(the `anthropic_api`/`bedrock` backends, RAG runner, `reconstruct build`), the
`anthropic[bedrock]` signing stack (boto3/botocore), and `claude-agent-sdk`
(the graph-agent runner). Because all are imported lazily, a missing declaration
slips past `import`-based tests and only explodes the first time someone runs
the real path.

This script imports exactly those runtime deps — the same symbols the runners
import at call time — so that a missing/undeclared dependency FAILS the build
instead of lurking until a live run. CI runs it after `uv sync --extra bench`
(see .github/workflows/ci.yml); run it locally the same way:

    uv sync --extra bench && uv run python scripts/import_smoke.py

It performs NO network I/O and needs NO API key: it only imports and constructs
the backend object (client creation stays lazy), so it is safe in CI.
"""

from __future__ import annotations


def main() -> None:
    # Gap #1: the base Anthropic SDK, imported lazily by the anthropic_api/bedrock
    # backends. Undeclared anywhere in pyproject before esim-sr3.
    import anthropic  # noqa: F401

    # Gap #3: the `bedrock` backend's signing stack. `AnthropicBedrock` ships in
    # the base SDK, but it signs each request with boto3/botocore, which only the
    # `anthropic[bedrock]` extra declares — undeclared before esim Bedrock, so the
    # first live Bedrock call raised ModuleNotFoundError. boto3 is imported lazily
    # (anthropic/lib/bedrock/_auth.py), so we import it here explicitly to fail the
    # build if the extra is missing.
    import boto3  # noqa: F401

    # The graph-agent runner module (pulls in the kuzu/pyoxigraph engine layer).
    import enterprise_sim.benchmark.runners.graph_agent  # noqa: F401
    from anthropic import AnthropicBedrock

    # Gap #2: the graph-agent runner's SDK — the exact symbols graph_agent imports
    # inside its agent loop, so this smoke fails identically if the SDK is missing.
    from claude_agent_sdk import (  # noqa: F401
        ClaudeAgentOptions,
        create_sdk_mcp_server,
        query,
        tool,
    )

    # Construct the Bedrock client the same way the backend does. Empirically
    # (anthropic 0.40+) the no-arg constructor does NOT raise without AWS env — it
    # logs "No AWS region specified, defaulting to us-east-1" — but we pass an
    # explicit region so the smoke stays quiet and region-independent. No creds and
    # no network: signing/dispatch only happen on an actual request.
    AnthropicBedrock(aws_region="us-east-1")

    # The anthropic_api backend must wire up (client creation stays lazy — no key
    # needed here): this is what `reconstruct build --backend anthropic_api` and
    # the RAG runner construct.
    from enterprise_sim.core.llm import LLMConfig, build_client

    build_client(LLMConfig(backend="anthropic_api"))

    # The bedrock backend wires up the same way (client creation stays lazy). It now
    # requires a Bedrock inference-profile model id — a 1P id fails fast at build time
    # (finding F2) — so we pass the inference-profile form the backend actually sends.
    build_client(LLMConfig(backend="bedrock", model="us.anthropic.claude-sonnet-4-6-20250929-v1:0"))

    # Gap #4: the stubbed request-path tests (tests/test_llm_sdk_path.py) duck-type the
    # SDK client, so they can't catch a real `anthropic` signature drift. Pin the seven
    # kwargs both SDK backends pass to `messages.create` (see `_call_tool` in
    # enterprise_sim/core/llm/backends.py) against the *installed* SDK: if the real
    # method stops accepting one, fail the build here — naming the vanished kwarg —
    # instead of at the first live call (finding F6). No network/key: signature only.
    import inspect

    from anthropic.resources.messages import Messages

    create_params = inspect.signature(Messages.create).parameters
    accepts_var_kwargs = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in create_params.values()
    )
    passed_kwargs = (
        "model",
        "max_tokens",
        "temperature",
        "system",
        "messages",
        "tools",
        "tool_choice",
    )
    missing = [name for name in passed_kwargs if name not in create_params]
    if missing and not accepts_var_kwargs:
        raise AssertionError(
            f"anthropic Messages.create no longer accepts {missing}; the SDK backends "
            "pass these to messages.create, so a live call would now fail. Reconcile "
            "enterprise_sim/core/llm/backends.py with the SDK, then this smoke."
        )

    print("real-LLM runtime import smoke: OK")


if __name__ == "__main__":
    main()
