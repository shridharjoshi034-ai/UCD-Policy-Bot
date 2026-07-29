#!/bin/bash
# export_db.sh
# This script exports the current state of the Langfuse Postgres database into a SQL file.

echo "Exporting Langfuse database..."
# Find the container ID for the langfuse-db service
CONTAINER_NAME=$(docker compose ps -q langfuse-db)

if [ -z "$CONTAINER_NAME" ]; then
    echo "Error: langfuse-db container is not running. Start it with 'docker compose up -d' first."
    exit 1
fi

# Run pg_dump inside the container and save it to langfuse_backup.sql
docker exec -t $CONTAINER_NAME pg_dump -U postgres -d langfuse > langfuse_backup.sql

echo "Database exported successfully to langfuse_backup.sql!"
echo "You can now commit langfuse_backup.sql (if it doesn't contain sensitive data) or share it directly with your team."
