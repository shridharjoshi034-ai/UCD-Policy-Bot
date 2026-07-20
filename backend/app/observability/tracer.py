from langfuse import Langfuse

# Initialize the centralized Langfuse client
langfuse_client = Langfuse()

def get_tracer():
    """
    Returns the initialized Langfuse client.
    Can be expanded later to wrap specific tracing logic.
    """
    return langfuse_client
