import json
import time
from urllib.request import urlopen
from core import DATA, storage_available
with urlopen('http://127.0.0.1:9001/healthz', timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
with urlopen('http://127.0.0.1:9997/v3/paths/list', timeout=2) as response:
    json.load(response)
worker = json.loads((DATA / 'worker.json').read_text())
# Disk pressure is an operational warning, not a reason to repeatedly restart containers.
if not storage_available() or not worker.get('ok') or time.time() - worker['checked_at'] > 240:
    raise SystemExit(1)
