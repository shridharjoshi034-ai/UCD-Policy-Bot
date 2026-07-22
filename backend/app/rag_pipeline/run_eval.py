import os
import sys
import json
import asyncio
from dotenv import load_dotenv

load_dotenv()

from app.observability.tracer import get_tracer
from app.rag_pipeline.retrieval import PolicyRAGPipeline

async def run_evaluation(dataset_name: str, run_name: str):
    print(f"Connecting to Langfuse to fetch dataset: {dataset_name}...")
    langfuse = get_tracer()
    
    try:
        dataset = langfuse.get_dataset(dataset_name)
    except Exception as e:
        print(f"Failed to fetch dataset '{dataset_name}'. Did you upload it with that exact name in Langfuse?")
        print(f"Error: {e}")
        return

    print(f"Successfully loaded dataset: {dataset_name} ({len(dataset.items)} items)")
    print(f"Starting evaluation run: {run_name}")

    pipeline = PolicyRAGPipeline()
    
    async def evaluate_item(item, **kwargs):
        print(f"\nEvaluating item...")
        
        question_data = item.input
        if isinstance(question_data, str):
            try:
                question_data = json.loads(question_data)
            except json.JSONDecodeError:
                question_data = {"question": question_data}
        
        query = question_data.get("question", str(question_data))
        print(f"Query: {query}")
        
        with langfuse.start_as_current_observation(name="chat_response", as_type="span") as trace:
            trace.update(input={"question": query})
            
            chunks = pipeline.retrieve(query_text=query, limit=5)
            
            answer_parts = []
            async for token in pipeline.generate_answer(query_text=query, retrieved_chunks=chunks):
                answer_parts.append(token)
                print(token, end="", flush=True)
                
            final_answer = "".join(answer_parts)
            
            citations = []
            seen_files = set()
            for chunk in chunks:
                file_name = chunk.get("source_file_name")
                if file_name and file_name not in seen_files:
                    seen_files.add(file_name)
                    citations.append({
                        "title": file_name,
                        "source_url": "#"
                    })
            
            trace.update(output={
                "answer": final_answer,
                "citations": citations,
                "contexts": [chunk["text"] for chunk in chunks]
            })
            
            print("\n[Done with item]")
            return final_answer
            
    try:
        dataset.run_experiment(
            name=run_name,
            task=evaluate_item,
            max_concurrency=1
        )
    finally:
        pipeline.close()
        langfuse.flush()

if __name__ == "__main__":
    # Allow passing dataset name as a CLI argument, otherwise default to ucd_policy_eval
    ds_name = sys.argv[1] if len(sys.argv) > 1 else "ucd_policy_eval"
    run_identifier = "local_qwen_eval_1"
    
    asyncio.run(run_evaluation(ds_name, run_identifier))
