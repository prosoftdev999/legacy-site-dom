#!/bin/sh
# Grades /app/output/page_dom.json against the trees Chromium built. The answer is only
# parsed as JSON; nothing the agent produced is executed.
set +e

mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt

python3 -m pytest -q -p no:cacheprovider /tests/test_dom.py --ctrf=/logs/verifier/ctrf.json
status=$?

if [ "$status" -eq 0 ]; then
    printf '1\n' > /logs/verifier/reward.txt
else
    printf '0\n' > /logs/verifier/reward.txt
fi
exit 0
