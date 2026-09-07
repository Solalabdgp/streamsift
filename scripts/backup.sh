#!/bin/sh
set -e

BACKUP_DIR=/backups
mkdir -p "$BACKUP_DIR"

while true; do
    NOW_H=$(date +%H)
    NOW_M=$(date +%M)
    if [ "$NOW_H" = "04" ] && [ "$NOW_M" = "00" ]; then
        STAMP=$(date +%Y%m%d_%H%M%S)
        echo "backup: pg_dump -> $BACKUP_DIR/jobradar_$STAMP.sql.gz"
        PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h postgres -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$BACKUP_DIR/jobradar_$STAMP.sql.gz"
        find "$BACKUP_DIR" -name '*.sql.gz' -mtime +14 -delete
        sleep 70
    fi
    sleep 30
done
