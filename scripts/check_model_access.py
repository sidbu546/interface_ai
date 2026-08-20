"""Preflight: confirm we can actually reach the model, before anything depends on it.

Makes one small real API call and prints the server-issued identifiers that
prove it happened -- the same fields the discovery run will record into its
journal as evidence.

The key is read from the ANTHROPIC_API_KEY environment variable. It is never
hardcoded, never logged, and never written to disk. If you ever find yourself
pasting a key into a file or a chat, rotate it.

Run:  python3 scripts/check_model_access.py
"""

from __future__ import annotations

import os
import sys

MODEL = "claude-opus-5"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.\n")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        print("\nAdd it to ~/.zshrc to persist across terminals.")
        return 2

    try:
        import anthropic
    except ImportError:
        print("The anthropic SDK is not installed.\n")
        print("  python3 -m pip install anthropic")
        return 2

    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        )
    except anthropic.AuthenticationError:
        print("FAIL - the API key was rejected. Check it is current and not revoked.")
        return 1
    except anthropic.NotFoundError:
        print(f"FAIL - model {MODEL!r} not available to this key.")
        return 1
    except anthropic.PermissionDeniedError:
        print(f"FAIL - this key lacks permission for {MODEL!r}.")
        return 1
    except anthropic.RateLimitError:
        print("FAIL - rate limited. The key works; try again shortly.")
        return 1
    except anthropic.APIConnectionError as exc:
        print(f"FAIL - could not reach the API: {exc}")
        return 1

    # Always check stop_reason before reading content: a safety refusal returns
    # HTTP 200 with empty content, and indexing content[0] would crash.
    if response.stop_reason == "refusal":
        print("Model declined this request (stop_reason=refusal).")
        print("Unexpected for a preflight, but the connection itself works.")
        return 1

    text = next((b.text for b in response.content if b.type == "text"), "")
    usage = response.usage

    print("OK - model access confirmed.\n")
    print(f"  model         {response.model}")
    print(f"  message_id    {response.id}")
    print(f"  request_id    {response._request_id}")
    print(f"  stop_reason   {response.stop_reason}")
    print(f"  input_tokens  {usage.input_tokens}")
    print(f"  output_tokens {usage.output_tokens}")
    print(f"  reply         {text.strip()!r}")
    print(
        "\nThe message_id and request_id are issued by Anthropic. Fields like these"
        "\nare what the discovery journal records as proof the run really happened."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
