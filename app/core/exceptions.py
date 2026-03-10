"""
Custom exception hierarchy for the Ingestion Worker.

Design: exceptions are specific enough that the worker task can decide
exactly how to handle each one — skip the document, fail the job, retry, etc.
"""


class WorkerError(Exception):
    """Base class for all worker exceptions."""

    pass


# ── Document-level errors (catch per document, job continues) ─────────────────


class UnsupportedDocumentTypeError(WorkerError):
    """
    Raised by LoaderFactory when no loader is registered for a MIME type.
    The worker catches this, marks the document as 'skipped', and continues.
    The job does NOT fail — one unsupported file shouldn't stop 500 others.
    """

    pass


class DocumentParsingError(WorkerError):
    """
    Raised when a loader fails to parse a document (corrupt file, encoding issue).
    Caught per-document — logged as a warning, job continues.
    """

    pass


class DocumentTooLargeError(WorkerError):
    """
    Raised when a file exceeds the configured max_file_size_mb limit.
    Caught per-document — document is skipped, job continues.
    """

    pass


# ── Job-level errors (cause the whole task to fail) ───────────────────────────


class RepoConfigNotFoundError(WorkerError):
    """
    Raised when the worker can't find the repo config in MongoDB.
    This is a job-level failure — can't proceed without the config.
    """

    pass


class ConnectorError(WorkerError):
    """
    Raised when a connector fails to connect to the data source.
    Job-level failure — can't fetch any documents.
    """

    pass


class EmbeddingError(WorkerError):
    """
    Raised when the embedding provider fails after all retries.
    Job-level failure.
    """

    pass


# ── Infrastructure errors ─────────────────────────────────────────────────────


class DatabaseConnectionError(WorkerError):
    """Raised when MongoDB is not reachable at worker startup."""

    pass
