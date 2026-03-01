"""Structured JSON logging via structlog.

Call ``configure_logging()`` once at application startup (API lifespan or
worker ``main()``) before obtaining any loggers.

Usage::

    from atlasqueue.core.logging import configure_logging, get_logger

    configure_logging(log_level="INFO", service="atlasqueue-api")
    logger = get_logger(__name__)
    logger.info("job_received", job_id=str(job_id))
"""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO", service: str = "atlasqueue") -> None:
    """Wire up structlog for JSON output.

    Must be called exactly once before any logger is obtained.  Idempotent on
    repeated calls (structlog.configure is thread-safe).
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Inject constant service name into every log record.
        _ServiceNameInjector(service),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Final processor: render to JSON.
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level.upper())

    # Suppress noisy libraries.
    for noisy in ("uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog-bound logger for *name*."""
    return structlog.get_logger(name)


class _ServiceNameInjector:
    """structlog processor that adds a fixed ``service`` key to every event."""

    def __init__(self, service: str) -> None:
        self._service = service

    def __call__(
        self,
        logger: object,
        method: str,
        event_dict: structlog.types.EventDict,
    ) -> structlog.types.EventDict:
        event_dict["service"] = self._service
        return event_dict
