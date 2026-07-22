import os
from qdrant_client import QdrantClient
from dotenv import load_dotenv

load_dotenv()
client = QdrantClient(url=os.getenv("QDRANT_URL"), api_key=os.getenv("QDRANT_API_KEY"))
print(client.count(collection_name="ucd_policies"))
