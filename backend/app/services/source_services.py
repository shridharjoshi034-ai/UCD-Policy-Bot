

def get_source_list():
    # This function should interact with the database or data storage to fetch the list of sources
    # For demonstration, we will return a static list of sources
    return [
        {
            "source_id": "1",
            "title": "Example Source 1",
            "source_url": "http://example.com/source1",
            "source_type": "pdf",
            "pages_indexed": 10,
            "last_indexed_at": "2024-06-01T12:00:00Z"
        }
    ]