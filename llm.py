"""One Claude client for every robot script.

- Key from ANTHROPIC_API_KEY in .env.
- IPv4 only: on the iPhone hotspot api.anthropic.com resolves to IPv6 first,
  IPv6 connects hang, and each call stalled 30-40 s before falling back.
- max_retries=0: a failure surfaces immediately instead of being retried.
- preflight(): one tiny call at startup, BEFORE the dog stands, so a broken
  key/network/library stops the run loudly instead of letting him walk blind.

(The original 'APIConnectionError' on every call was Brotli 1.1.0 in the
venv: httpx2 needs brotli>=1.2.0 to decode the API's compressed responses.)
"""
import anthropic
import httpx2

from envfile import load_env

TIMEOUT_S = 60


class ApiFatal(Exception):
    """An API failure the scripts treat as fatal: stop the dog and exit."""


def make_client():
    key = load_env().get("ANTHROPIC_API_KEY")
    if not key:
        raise ApiFatal("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.AsyncAnthropic(
        api_key=key,
        max_retries=0,
        timeout=TIMEOUT_S,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.AsyncHTTPTransport(local_address="0.0.0.0")),
    )


def describe(err):
    cause = f" (cause: {type(err.__cause__).__name__}: {err.__cause__})" if err.__cause__ else ""
    return f"{type(err).__name__}: {err}{cause}"


async def preflight(client, model):
    """Raise ApiFatal unless one minimal request to `model` succeeds."""
    try:
        await client.messages.create(
            model=model, max_tokens=64, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the word ok."}])
    except anthropic.APIError as e:
        raise ApiFatal(f"API preflight to {model} failed: {describe(e)}") from e


def loud(msg):
    bar = "!" * 72
    return f"\n{bar}\n!! API FAILURE - STOPPING THE DOG\n!! {msg}\n{bar}"
