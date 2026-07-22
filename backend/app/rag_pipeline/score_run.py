import os
import sys
import asyncio
from dotenv import load_dotenv

load_dotenv()

from app.observability.tracer import get_tracer
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

# We'll use Structured Output to guarantee a clean score format
class EvalScore(BaseModel):
    score: float = Field(description="A float score between 0.0 and 1.0 indicating correctness.")
    reasoning: str = Field(description="A brief explanation for the score.")

async def score_evaluation_run(dataset_name: str, run_name: str):
    langfuse = get_tracer()
    
    try:
        dataset = langfuse.get_dataset(dataset_name)
    except Exception as e:
        print(f"Failed to fetch dataset '{dataset_name}'. Error: {e}")
        return

    print(f"Fetching traces for dataset: {dataset_name}, run: {run_name}...")
    
    # Langchain OpenAI client
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(EvalScore)
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an expert evaluator judging a question-answering system.
Your task is to compare the generated answer against the expected ground-truth answer.

Evaluate whether the Generated Answer is factually correct and conveys the same core information as the Expected Answer. 
It does not need to be an exact word-for-word match.

Score the answer:
1.0 = Completely correct
0.5 = Partially correct (missing details but not wrong)
0.0 = Incorrect or irrelevant"""),
        ("user", """Question: {question}

Expected Answer: {expected_answer}

Generated Answer: {actual_answer}""")
    ])
    
    eval_chain = prompt | llm

    traces_scored = 0
    
    for item in dataset.items:
        # Get the trace linked to this item for this specific run
        # Note: In the Langfuse SDK, the dataset item linked traces can be retrieved via the API.
        # But wait, dataset.items does not have the traces attached directly in all SDK versions.
        # Let's just fetch all traces with the tag/metadata of this run_name.
        pass
        
    print("This script needs to query Langfuse traces, which is easier via the Langfuse API client.")

if __name__ == "__main__":
    pass
