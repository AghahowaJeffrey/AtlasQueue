"""Job handler registry.

A handler is an async callable that receives the job payload dict and
either returns (success) or raises (failure).  Raise any exception to
signal failure — the executor will handle retry/DLQ logic.

Usage
-----
Register a handler at module import time (similar to Celery task decorators)::

    from atlasqueue.worker.handlers import register

    @register("email.send")
    async def send_email(payload: dict) -> None:
        to = payload["to"]
        ...   # call mail service

The registry is a plain module-level dict — importable and testable without
any framework overhead.
"""
from __future__ import annotations

from collections.abc import Callable, Awaitable
from typing import TypeAlias

# Handler signature: receives raw payload dict, returns nothing on success,
# raises on failure.
Handler: TypeAlias = Callable[[dict], Awaitable[None]]

_registry: dict[str, Handler] = {}


def register(job_type: str) -> Callable[[Handler], Handler]:
    """Decorator: register *fn* as the handler for *job_type*."""

    def decorator(fn: Handler) -> Handler:
        _registry[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> Handler | None:
    """Look up the handler for *job_type*. Returns ``None`` if unregistered."""
    return _registry.get(job_type)


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------

@register("noop")
async def _noop_handler(payload: dict) -> None:  # noqa: ARG001
    """No-op handler — used for smoke tests and local dev.

    Succeeds immediately without performing any work.
    """
