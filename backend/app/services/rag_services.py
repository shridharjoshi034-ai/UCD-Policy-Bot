import time

def answer_question(question : str) -> dict:
    # Simulate some processing time
    time.sleep(1)

    return {
        "answer": f"This is a mock answer to the question: {question}",
        "citations": [],
        "verification_status": "not_run",
        "latency_ms": 1000
    }
