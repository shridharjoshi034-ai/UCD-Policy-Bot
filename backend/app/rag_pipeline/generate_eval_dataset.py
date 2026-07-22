import os
import json
import random
import httpx
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

# Configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL_NAME = os.getenv("OLLAMA_MODEL_NAME", "qwen2.5:1.5b")

SCRIPT_DIR = Path(__file__).parent
MDS_DIR = SCRIPT_DIR / "policies_mds"
OUTPUT_FILE = SCRIPT_DIR.parent.parent / "langfuse_eval_dataset.json"

TARGET_QUESTIONS = 100
QUESTIONS_PER_CHUNK = 3

SYSTEM_PROMPT = """
You are a synthetic data generator creating an evaluation dataset for a RAG system.
Based ONLY on the provided text chunk, generate realistic questions that a user might ask, along with the correct expected answers derived purely from the text.
Output strictly in JSON format matching this exact schema:
{
  "qa_pairs": [
    {
      "input": {"question": "The question here"},
      "expected_output": {"answer": "The answer here"}
    }
  ]
}
"""

async def generate_questions(text_chunk: str) -> list:
    url = f"{OLLAMA_BASE_URL}/api/chat"
    
    payload = {
        "model": OLLAMA_MODEL_NAME,
        "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Text chunk:\n\n{text_chunk}\n\nGenerate {QUESTIONS_PER_CHUNK} question and answer pairs."}
        ],
        "stream": False,
        "options": {
            "temperature": 0.3
        }
    }
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()
            content = result.get("message", {}).get("content", "{}")
            
            data = json.loads(content)
            return data.get("qa_pairs", [])
        except Exception as e:
            print(f"Error generating or parsing questions: {e}")
            return []

async def main():
    if not MDS_DIR.exists():
        print(f"Error: {MDS_DIR} not found.")
        return

    md_files = list(MDS_DIR.glob("*.md"))
    if not md_files:
        print(f"Error: No markdown files found in {MDS_DIR}.")
        return

    print(f"Found {len(md_files)} markdown files. Starting generation...")

    # We want varied chunks. Let's load a bunch of files, split them, and sample chunks.
    all_chunks = []
    char_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=0)
    
    for file_path in md_files:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            # Ignore empty or very short files
            if len(content) < 500: continue
            splits = char_splitter.split_text(content)
            all_chunks.extend(splits)
            
    print(f"Created {len(all_chunks)} text chunks.")
    
    # Shuffle chunks to get diversity across different policies
    random.shuffle(all_chunks)
    
    dataset = []
    chunks_processed = 0
    
    for chunk in all_chunks:
        if len(dataset) >= TARGET_QUESTIONS:
            break
            
        print(f"Processing chunk {chunks_processed + 1}...")
        qa_pairs = await generate_questions(chunk)
        
        for pair in qa_pairs:
            if "input" in pair and "expected_output" in pair:
                dataset.append(pair)
                
        chunks_processed += 1
        print(f"Total questions generated so far: {len(dataset)}/{TARGET_QUESTIONS}")

    # Truncate to exactly TARGET_QUESTIONS if we overshot
    dataset = dataset[:TARGET_QUESTIONS]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2)
        
    print(f"\nSuccessfully generated {len(dataset)} questions!")
    print(f"Dataset saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
