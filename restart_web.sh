#!/usr/bin/env bash
# Надёжный перезапуск дашборда. Появился из живого инцидента: перезапуски убивали один
# процесс из связки nohup → pegasgap.exe → python, порт оставался у выжившего, и обход
# сутки работал на старом коде, а стадия hotelcheck «не работала».
set -u
cd "$(dirname "$0")"

echo "1/4 штатный стоп обхода (доводит текущий кейс)"
python - <<'PY' 2>/dev/null || true
import json, urllib.request
r = urllib.request.Request('http://127.0.0.1:8000/api/worker/stop', data=b'{}',
                           headers={'Content-Type': 'application/json'})
json.load(urllib.request.urlopen(r, timeout=500))
PY

echo "2/4 убить ВСЕ web-процессы по командной строке (не по одному pid)"
python - <<'PY'
import re, subprocess
out = subprocess.check_output(['wmic', 'process', 'get', 'ProcessId,CommandLine'],
                              text=True, errors='replace')
for line in out.splitlines():
    low = line.lower()
    if 'pegasgap' in low and ' web' in low and 'wmic' not in low:
        m = re.search(r'(\d+)\s*$', line.strip())
        if m:
            subprocess.run(['taskkill', '/PID', m.group(1), '/F', '/T'],
                           capture_output=True)
PY
sleep 3

if netstat -ano | grep ":8000" | grep -q LISTENING; then
    echo "порт 8000 всё ещё занят — перезапуск прерван"; exit 1
fi

echo "3/4 старт одного процесса"
nohup ./.venv/Scripts/pegasgap.exe web > /dev/null 2>&1 &
sleep 10

echo "4/4 контроль"
python - <<'PY'
import json, urllib.request
print('healthz:', json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=60)))
PY
