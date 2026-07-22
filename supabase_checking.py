#!/usr/bin/env python3
"""
List all files in a Supabase Storage bucket recursively.
Reads SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and SUPABASE_BUCKET
from a .env file found in the current directory or any subfolder.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client, Client

def find_dotenv(start_path: str = ".") -> str | None:
    """
    Recursively search for a .env file starting from start_path.
    Returns the path of the first .env file found, or None if not found.
    """
    start = Path(start_path).resolve()
    for root, dirs, files in os.walk(start):
        # Skip hidden directories to speed up search
        dirs[:] = [d for d in dirs if not d.startswith('.') or d == '.env']
        if ".env" in files:
            return os.path.join(root, ".env")
    return None

def list_folder(supabase: Client, bucket_name: str, prefix: str = "") -> list[dict]:
    """
    Retrieve all items (files and folders) at the given prefix, handling pagination.
    """
    items = []
    limit = 100
    offset = 0
    while True:
        try:
            response = supabase.storage.from_(bucket_name).list(
                prefix,
                options={
                    "limit": limit,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
        except Exception as e:
            print(f"Error listing '{prefix}': {e}")
            break

        if not response:
            break
        items.extend(response)
        if len(response) < limit:
            break
        offset += limit
    return items

def list_all_files(supabase: Client, bucket_name: str, prefix: str = "") -> list[str]:
    """
    Recursively list all file paths in the bucket.
    """
    all_files = []
    items = list_folder(supabase, bucket_name, prefix)

    for item in items:
        if item.get("id") is None:  # Folder
            folder_prefix = prefix + item["name"] + "/"
            all_files.extend(list_all_files(supabase, bucket_name, folder_prefix))
        else:  # File
            file_path = prefix + item["name"]
            all_files.append(file_path)

    return all_files

def main():
    # Find .env file recursively
    dotenv_path = find_dotenv()
    if dotenv_path is None:
        print("Error: No .env file found in current directory or subdirectories.")
        return

    print(f"Loading environment from: {dotenv_path}")
    load_dotenv(dotenv_path)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    bucket_name = os.environ.get("SUPABASE_BUCKET")

    if not all([url, key, bucket_name]):
        print(
            "Error: Please set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, "
            "and SUPABASE_BUCKET in your .env file."
        )
        return

    supabase: Client = create_client(url, key)

    print(f"Scanning bucket '{bucket_name}'...")
    files = list_all_files(supabase, bucket_name)

    if files:
        print(f"\nFound {len(files)} file(s):")
        for f in files:
            print(f)
    else:
        print("No files found.")

if __name__ == "__main__":
    main()