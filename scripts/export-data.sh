#!/bin/sh
set -eu

source_dir=/srv/apps/landing-page/data
export_dir=/srv/backups/exports/landing-page
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$export_dir/control-center-data-$timestamp.tar.gz"
temporary="$archive.tmp"

if [ ! -d "$source_dir" ]; then
    echo "data directory not found: $source_dir" >&2
    exit 1
fi

umask 077
mkdir -p "$export_dir"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
tar -C "$source_dir" -czf "$temporary" .
mv "$temporary" "$archive"
trap - EXIT HUP INT TERM

echo "$archive"
