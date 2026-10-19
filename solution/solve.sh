#!/bin/sh
set -eu
python3 "$(dirname "$0")/solve.py" /app/data /app/output/page_dom.json
