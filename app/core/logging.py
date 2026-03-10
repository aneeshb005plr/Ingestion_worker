"""
Identical logging setup to the orchestrator.
See orchestrator/app/core/logging.py for full explanation.

Worker-specific note:
  In ARQ tasks, bind job context at the start of every task:
    log = structlog.get_logger().bind(job_id=job_id, tenant_id=tenant_id, repo_id=repo_id)
  Every log line from that task will carry these fields automatically.
"""

import logging
import sys
import structlog
from app.core.config import settings


_SENSITIVE_KEYS = frozenset(
    {
        "client_secret",
        "password",
        "api_key",
        "token",
        "authorization",
        "connection_string",
        "mongo_uri",
        "redis_uri",
        "openai_api_key",
    }
)


def _scrub_sensitive_data(logger: object, method: str, event_dict: dict) -> dict:
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def setup_logging() -> None:
    is_production = settings.ENVIRONMENT == "production"
    log_level = logging.INFO if is_production else logging.DEBUG

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _scrub_sensitive_data,
        structlog.processors.StackInfoRenderer(),
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if is_production
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("arq").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("docling").setLevel(logging.WARNING)
