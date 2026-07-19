import os
import traceback
from dotenv import load_dotenv
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException

# ============================================================
# Load .env from the 'backend' folder
# ============================================================
script_dir = Path(__file__).parent
env_path = script_dir / "backend" / ".env"

if env_path.exists():
    load_dotenv(env_path)
else:
    print("❌ .env file not found in backend/ folder.")
    exit(1)

url = os.getenv("QDRANT_URL")
api_key = os.getenv("QDRANT_API_KEY")

if not url:
    print("❌ QDRANT_URL not set in .env")
    exit(1)

# ============================================================
# Helper: Display collections
# ============================================================
def display_collections(client):
    """Fetch and print all collections with details."""
    collections = client.get_collections().collections
    if not collections:
        print("\n📭 No collections found.")
        return []

    print("\n" + "=" * 60)
    print(f"Found {len(collections)} collection(s):\n")
    collection_list = []
    for idx, col in enumerate(collections, 1):
        try:
            info = client.get_collection(collection_name=col.name)
            vectors_cfg = info.config.params.vectors
            sparse_cfg = info.config.params.sparse_vectors

            # Handle both named-vectors (dict) and single-vector (object) configs
            if isinstance(vectors_cfg, dict):
                vector_names = list(vectors_cfg.keys())
                dims = [v.size for v in vectors_cfg.values()]
                dim_label = ", ".join(f"{n}:{d}" for n, d in zip(vector_names, dims))
            else:
                dim_label = str(vectors_cfg.size)

            points_count = info.points_count
            indexed_vectors_count = getattr(info, "indexed_vectors_count", 0)

            # Count unique source files
            unique_files = set()
            offset = None
            while True:
                points, next_offset = client.scroll(
                    collection_name=col.name,
                    limit=1000,
                    offset=offset,
                    with_payload=["source_file_name"],
                    with_vectors=False
                )
                if not points:
                    break
                for point in points:
                    file_name = point.payload.get("source_file_name")
                    if file_name:
                        unique_files.add(file_name)
                offset = next_offset
                if offset is None:
                    break

            print(f"{idx}. {col.name}")
            print(f"   Vectors: {dim_label}")
            if sparse_cfg:
                print(f"   Sparse: {list(sparse_cfg.keys()) if isinstance(sparse_cfg, dict) else 'yes'}")
            print(f"   Total Chunks (Points): {points_count}")
            print(f"   Unique Source Files: {len(unique_files)}")
            print()
            collection_list.append(col.name)
        except Exception as e:
            print(f"{idx}. {col.name} (could not fetch details: {e})")
            traceback.print_exc()
            collection_list.append(col.name)
    print("=" * 60)
    return collection_list

# ============================================================
# Helper: Delete a collection with confirmation
# ============================================================
def delete_collection(client, collection_name):
    """Delete a collection after user confirmation."""
    confirm = input(f"⚠️  Are you sure you want to delete collection '{collection_name}'? This cannot be undone. (y/n): ")
    if confirm.lower() == 'y':
        try:
            client.delete_collection(collection_name=collection_name)
            print(f"✅ Collection '{collection_name}' deleted successfully.")
        except Exception as e:
            print(f"❌ Failed to delete collection '{collection_name}': {e}")
    else:
        print("❌ Deletion cancelled.")

# ============================================================
# Main
# ============================================================
try:
    client = QdrantClient(url=url, api_key=api_key)
except Exception as e:
    print(f"❌ Cannot connect to Qdrant: {e}")
    exit(1)

while True:
    # Display current collections
    collection_names = display_collections(client)

    if not collection_names:
        print("No collections to delete.")
        break

    # Ask if user wants to delete
    choice = input("\n🗑️  Do you want to delete a collection? (y/n): ").lower()
    if choice != 'y':
        print("Exiting without deletions.")
        break

    # Ask which collection (by number)
    try:
        idx = int(input("Enter the number of the collection to delete: "))
        if idx < 1 or idx > len(collection_names):
            print(f"❌ Invalid number. Please enter a number between 1 and {len(collection_names)}.")
            continue
        collection_to_delete = collection_names[idx - 1]
    except ValueError:
        print("❌ Please enter a valid number.")
        continue

    # Confirm and delete
    delete_collection(client, collection_to_delete)

    # After deletion, loop will show updated list again
    print("\n📋 Updated collection list:\n")

# Final display after loop
print("\n📋 Final collection list:")
display_collections(client)