from langfuse import get_client

def get_tracer():
    """
    Returns the initialized Langfuse client.
    Can be expanded later to wrap specific tracing logic.
    """
    return get_client()
