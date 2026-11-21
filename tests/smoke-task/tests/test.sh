#!/bin/sh
set -eu
mkdir -p /logs/verifier
reward=0
if [ "$(cat /tmp/dsh-smoke-answer 2>/dev/null)" = DSH_OK ]; then
    reward=1
fi
echo "$reward" > /logs/verifier/reward.txt
