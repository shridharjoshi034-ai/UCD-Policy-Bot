import logging

log = logging.getLogger("tracer")

_langfuse_client = None
_init_failed = False


def get_tracer():
    """
    Returns the initialized Langfuse client, or a no-op wrapper if
    Langfuse credentials are not configured.
    """
    global _langfuse_client, _init_failed

    if _langfuse_client is not None:
        return _langfuse_client

    if _init_failed:
        return _NoopLangfuse()

    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse()
        return _langfuse_client
    except Exception as e:
        _init_failed = True
        log.warning(f"Langfuse unavailable (tracing disabled): {e}")
        return _NoopLangfuse()


class _NoopLangfuse:
    """No-op Langfuse client that silently discards all tracing calls."""

    def trace(self, *args, **kwargs):
        return _NoopSpan()

    def flush(self):
        pass


class _NoopSpan:
    """No-op span that silently discards all update/end calls."""

    def span(self, *args, **kwargs):
        return _NoopSpan()

    def generation(self, *args, **kwargs):
        return _NoopSpan()

    def update(self, *args, **kwargs):
        pass

    def end(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass
