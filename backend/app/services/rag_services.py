import time

def answer_question(question : str, pipeline) -> dict:

    answer_parts = []

    started = time.perf_counter()

    for event in pipeline.stream_answer(question):
        if event["type"] == "token":
            answer_parts.append(event["text"])  # Append the token text to the answer_parts list
        
    return {
        "answer": "".join(answer_parts),
        "citations": [],
        "verification_status": "not_run",
        "latency_ms" : int((time.perf_counter() - started) * 1000)    
    }
