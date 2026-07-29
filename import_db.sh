#!/bin/bash
# import_db.sh
# This script imports a Langfuse database dump into the local Postgres container.

if [ ! -f "langfuse_backup.sql" ]; then
    echo "Error: langfuse_backup.sql not found!"
    echo "Please ensure the backup file is in the same directory as this script."
    exit 1
fi

echo "Importing Langfuse database..."
# Find the container ID for the langfuse-db service
CONTAINER_NAME=$(docker compose ps -q langfuse-db)

if [ -z "$CONTAINER_NAME" ]; then
    echo "Error: langfuse-db container is not running. Start it with 'docker compose up -d' first."
    exit 1
fi

# Drop and recreate the database to ensure a clean import, then run the SQL file
docker exec -i $CONTAINER_NAME psql -U postgres -c "DROP DATABASE IF EXISTS langfuse;"
docker exec -i $CONTAINER_NAME psql -U postgres -c "CREATE DATABASE langfuse;"
cat langfuse_backup.sql | docker exec -i $CONTAINER_NAME psql -U postgres -d langfuse

echo "Database imported successfully!"
