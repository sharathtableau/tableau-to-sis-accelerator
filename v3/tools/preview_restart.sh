#!/bin/sh
# Release 27: restart tools/preview_app.py on port 8601 (fresh modules + parse cache) and warm it up once.
cd "$(dirname "$0")/.." || exit 1
pid=$(netstat -ano | grep ":8601 " | grep LISTEN | awk '{print $5}' | head -1)
[ -n "$pid" ] && taskkill //F //PID "$pid" >/dev/null 2>&1
(python -m streamlit run tools/preview_app.py --server.port 8601 --server.headless true --server.address 127.0.0.1 --server.runOnSave false >/dev/null 2>&1 &)
for i in $(seq 1 60); do curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8601/ 2>/dev/null | grep -q 200 && break; sleep 1 2>/dev/null || timeout 1 cat /dev/zero >/dev/null 2>&1; done
echo "preview up on 8601"
