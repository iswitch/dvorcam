"""Recovery checks for the named local smoke stack only."""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from smoke import fetch
from urllib.request import urlopen

project = os.environ.get('DVORCAM_TEST_PROJECT', 'dvorcam-smoke')
compose = ['docker-compose', '-p', project, '-f', str(Path(__file__).with_name('compose.smoke.yaml'))]
container = project + '-dvorcam-1'


def inside(code):
    return subprocess.check_output(['docker', 'exec', container, 'python', '-c', code], text=True)


if __name__ == '__main__':
    page = fetch('/admin/').decode()
    token = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    fetch('/admin/camera', {'csrf': token, 'id': 'synthetic', 'editing': 'synthetic', 'name': 'Synthetic test camera', 'record': 'on'})
    time.sleep(8)
    subprocess.run(compose + ['stop', 'feed'], check=True, stdout=subprocess.DEVNULL)
    try:
        time.sleep(8)
        paths = json.loads(inside("import json; from urllib.request import urlopen; print(urlopen('http://127.0.0.1:9997/v3/paths/list').read().decode())"))
        camera = next(p for p in paths['items'] if p['name'] == 'synthetic-hd')
        assert camera['available'] is True and camera['online'] is False, camera
        subprocess.run(['docker', 'exec', '-e', 'PYTHONPATH=/tmp/webrtc-test', container, 'python', '/tmp/webrtc.py'], check=True)
        count = len(json.loads(fetch('/archive/synthetic/'))['clips'])
        time.sleep(8)
        assert len(json.loads(fetch('/archive/synthetic/'))['clips']) == count
        with urlopen('http://127.0.0.1:18880/synthetic-hd.jpg/status/') as response:
            status = json.load(response)
        assert not status['live'] and status['has_image']
        assert fetch('/synthetic-hd.jpg/')[:2] == b'\xff\xd8'
        print('PASS: NO SIGNAL remains playable, is not recorded; JPG falls back to disk', flush=True)
    finally:
        subprocess.run(compose + ['start', 'feed'], check=True, stdout=subprocess.DEVNULL)
    inside("from pathlib import Path; Path('/archive/.dvorcam-storage').rename('/archive/.storage-offline')")
    try:
        time.sleep(6)
        configuration = json.loads(inside("from urllib.request import urlopen; print(urlopen('http://127.0.0.1:9997/v3/config/paths/get/synthetic-hd').read().decode())"))
        assert configuration['record'] is False
        assert fetch('/synthetic-hd.jpg/')[:2] == b'\xff\xd8'
        subprocess.run(['docker', 'restart', container], check=True, stdout=subprocess.DEVNULL)
        for _ in range(30):
            try:
                assert fetch('/synthetic-hd.jpg/')[:2] == b'\xff\xd8'
                break
            except Exception:
                time.sleep(2)
        else:
            raise AssertionError('Live did not restart without the archive marker')
        print('PASS: missing storage marker stops recording, live/JPG restart without archive', flush=True)
    finally:
        inside("from pathlib import Path; Path('/archive/.storage-offline').rename('/archive/.dvorcam-storage')")
    time.sleep(6)
    configuration = json.loads(inside("from urllib.request import urlopen; print(urlopen('http://127.0.0.1:9997/v3/config/paths/get/synthetic-hd').read().decode())"))
    assert configuration['record'] is True
    subprocess.run(['docker', 'restart', container], check=True, stdout=subprocess.DEVNULL)
    for _ in range(30):
        try:
            data = json.loads(fetch('/archive/synthetic/'))
            if data['recording_enabled'] and data['clips']:
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        raise SystemExit('Restart did not preserve the camera and archive')
    subprocess.run(['docker', 'exec', '-u', '1000:1000', container, 'python', '-c',
        "from pathlib import Path; p=Path('/archive/.permission-test'); p.write_text('test'); p.rename('/archive/.permission-renamed'); Path('/archive/.permission-renamed').unlink()"], check=True)
    print('PASS: storage restoration, restart persistence, service-user write/rename/remove', flush=True)
