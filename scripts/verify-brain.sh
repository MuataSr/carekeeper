#!/usr/bin/env bash
# Rig Keeper Phase 0 verify: start edge brain, run real LLM report, stop brain.
set -u
SERVER=~/llama.cpp/build-cpu/bin/llama-server
MODEL="${MODEL:-$HOME/.nanobot/models/granite/ibm-granite_granite-4.1-3b-Q4_K_M.gguf}"
PORT=8084
LOG=/tmp/ls-run.log

"$SERVER" -m "$MODEL" --host 127.0.0.1 --port $PORT -c 4096 --threads 4 > "$LOG" 2>&1 &
SRV_PID=$!
echo "brain pid: $SRV_PID"

ok=0
for i in $(seq 1 40); do
  if curl -s --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then
    echo "BRAIN READY (try $i)"
    ok=1
    break
  fi
  sleep 3
done
if [ "$ok" != "1" ]; then
  echo "BRAIN NOT READY"; tail -5 "$LOG"; kill "$SRV_PID" 2>/dev/null; exit 1
fi

cd ~/rig-keeper && python3 care_agent.py --report 2>/dev/null | sed -n '/PLAIN-LANGUAGE/,$p'
RC=$?
kill "$SRV_PID" 2>/dev/null
wait "$SRV_PID" 2>/dev/null
echo "(brain stopped, report exit=$RC)"
exit $RC
