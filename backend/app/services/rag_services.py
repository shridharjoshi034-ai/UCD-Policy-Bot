import time

async def answer_question(question : str, pipeline) -> dict:

    answer_parts = []
    citations = []

    started = time.perf_counter()

    async for event in pipeline.stream_answer(question):
        if event["type"] == "token":
            answer_parts.append(event["text"])
        elif event["type"] == "final":
            citations = event.get("citations", [])
        
    return {
        "answer": "".join(answer_parts),
        "citations": citations,
        "verification_status": "not_run",
        "latency_ms" : int((time.perf_counter() - started) * 1000)
    }
