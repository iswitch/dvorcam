"""Restart checks against the isolated compose.smoke.yaml stack."""
from io import BytesIO
import json
import os
import subprocess
import time
from PIL import Image
from smoke import fetch

project = os.environ.get('DVORCAM_TEST_PROJECT', 'dvorcam-smoke')
container = project + '-dvorcam-1'
image = subprocess.check_output(['docker', 'inspect', container, '--format', '{{.Config.Image}}'], text=True).strip()
subprocess.run(['docker', 'stop', container], check=True, stdout=subprocess.DEVNULL)
try:
    # Damage only previews belonging to completed recordings, with the service stopped.
    code = '''import hashlib,json
from pathlib import Path
from core import ARCHIVE,entries
rows=entries('synthetic'); assert rows
paths=[]
for row in rows:
 for bucket in range(int(row['start']//15)*15, int((row['end']-0.001)//15)*15+1,15):
  path=ARCHIVE/'.previews/synthetic'/(str(bucket)+'.jpg')
  if path.is_file() and path not in paths: paths.append(path)
assert len(paths)>=2
videos={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ARCHIVE/'.ready/synthetic').glob('*.mp4')}
paths[0].unlink(); paths[1].write_bytes(b'broken JPEG')
(paths[0].parent/'interrupted.tmp.jpg').write_bytes(b'incomplete')
print(json.dumps({'times':[int(p.stem) for p in paths[:2]],'videos':videos}))'''
    result = subprocess.check_output(['docker', 'run', '--rm', '--user', '1000:1000', '--volumes-from', container,
                                     '--entrypoint', 'python', image, '-c', code], text=True)
    damaged = json.loads(result)
finally:
    subprocess.run(['docker', 'start', container], check=True, stdout=subprocess.DEVNULL)

for _ in range(60):
    try:
        metadata=json.loads(fetch('/archive/synthetic/'))
        size=(metadata['preview']['width'],metadata['preview']['height'])
        for stamp in damaged['times']:
            with Image.open(BytesIO(fetch('/archive/synthetic/preview/?time='+str(stamp)))) as jpeg:
                jpeg.load()
                assert jpeg.size == size and jpeg.width == 240
        break
    except Exception:
        time.sleep(1)
else:
    raise AssertionError('Startup did not repair previews')
current = json.loads(subprocess.check_output(['docker', 'exec', container, 'python', '-c',
    "import hashlib,json;from pathlib import Path; print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/archive/.ready/synthetic').glob('*.mp4')}))"], text=True))
assert all(current.get(p)==digest for p,digest in damaged['videos'].items())
subprocess.run(['docker','exec','-u','1000:1000',container,'python','/app/healthcheck.py'],check=True)
subprocess.run(['docker','exec',container,'python','-c',
                "from pathlib import Path; assert not list(Path('/archive/.previews').glob('*/*.tmp.jpg'))"],check=True)
print('PASS: container startup repairs missing/corrupt previews; JPEG HTTP, MP4 hashes and health check verified')
