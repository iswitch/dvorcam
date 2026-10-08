"""Isolated loopback RTSP check; requires MEDIAMTX_BIN and ffmpeg, no Docker/production."""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import yaml

root = Path(__file__).resolve().parents[1]
work = Path(tempfile.mkdtemp(prefix='dvorcam-native-smoke-'))
os.environ.update(DVORCAM_DATA=str(work / 'data'), DVORCAM_ARCHIVE=str(work / 'archive'),
                  DVORCAM_SNAPSHOTS=str(work / 'ram'), PUBLIC_URL='http://localhost:19001',
                  ADMIN_USERNAME='admin', ADMIN_PASSWORD='local-smoke-password')
sys.path.insert(0, str(root / 'app'))
import core

for path in (core.DATA / 'runtime', core.DATA / 'fallback', core.LAST_FRAMES, core.SNAPSHOTS, core.ARCHIVE):
    path.mkdir(parents=True, exist_ok=True)
core.atomic_write(core.DATA / 'storage-id', 'native-storage')
core.atomic_write(core.ARCHIVE / '.dvorcam-storage', 'native-storage')
fallback = core.DATA / 'fallback/cam.mp4'
subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:size=320x180:rate=10',
                '-t', '2', '-c:v', 'libx264', '-bf', '0', '-g', '10', '-threads', '1', str(fallback)], check=True)
value = {'version': 2, 'settings': dict(core.DEFAULT_SETTINGS), 'cameras': [
    {'id': 'cam', 'name': 'Native camera', 'record': True, 'streams': {'sd': {
        'rtsp': 'rtsp://127.0.0.1:18554/test', 'fallback_file': fallback.name}}}]}
core.atomic_json(core.DATA / 'state.json', value)
core.apply_media(value, paused=True)  # Validate the actual app configuration with the pinned binary.
config = core.media_config(value, paused=False)
config['pathDefaults']['recordSegmentDuration'] = '3s'
config['paths']['cam-sd']['runOnOnline'] = f'{sys.executable} {root}/app/snapshot.py cam-sd'
config['webrtcAddress'] = '127.0.0.1:8889'
config['webrtcLocalUDPAddress'] = '127.0.0.1:8189'
config['webrtcLocalTCPAddress'] = '127.0.0.1:8189'
configpath = core.DATA / 'runtime/native.yml'
core.atomic_write(configpath, yaml.safe_dump(config))
camera = work / 'camera.yml'
camera.write_text(yaml.safe_dump({'rtspAddress': '127.0.0.1:18554', 'rtspTransports': ['tcp'],
    'rtmp': False, 'hls': False, 'webrtc': False, 'srt': False, 'moq': False,
    'paths': {'test': {'source': 'publisher'}}}))
processes = []
log = (work / 'processes.log').open('w')


def wait(check, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(.25)
    raise AssertionError('Timed out; inspect ' + str(work / 'processes.log'))


def launch(args):
    process = subprocess.Popen(args, env=os.environ.copy(), stdout=log, stderr=log, start_new_session=True)
    processes.append(process)
    return process


try:
    launch([core.MEDIAMTX, str(camera)])
    media = launch([core.MEDIAMTX, str(configpath)])
    wait(lambda: urlopen('http://127.0.0.1:9997/v3/paths/list', timeout=2).close() is None)
    feed = launch(['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=10',
        '-c:v', 'libx264', '-bf', '0', '-g', '10', '-threads', '1', '-f', 'rtsp', '-rtsp_transport', 'tcp',
        'rtsp://127.0.0.1:18554/test'])
    saved = core.LAST_FRAMES / 'cam-sd.jpg'
    wait(saved.is_file)
    import web
    web.app.config['TESTING'] = True
    client = web.app.test_client()
    assert client.get('/cam-sd.jpg/status/').json['live']
    auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:local-smoke-password').decode()}
    page = client.get('/admin/?edit=cam', headers=auth)
    assert page.status_code == 200 and 'retention_days' in page.text
    (work / 'admin.html').write_text(page.text)
    player = client.get('/cam-sd/')
    assert player.status_code == 200 and 'dvorcam-frame-status' in player.text
    assert '/cam-sd.jpg/status/' in player.text
    # macOS has no Linux /proc recorder inventory: exercise real reload here, worker recovery in pytest.
    (core.ARCHIVE / '.dvorcam-storage').unlink()
    config['paths']['cam-sd']['record'] = False
    core.atomic_write(configpath, yaml.safe_dump(config))
    time.sleep(3)
    assert media.poll() is None and client.get('/cam-sd.jpg/status/').json['live']
    assert client.get('/cam-sd.jpg/').status_code == 200
    print('PASS real RTSP -> keyframe JPEG; live continues with archive disabled', flush=True)
    saved_digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    feed.terminate()
    feed.wait(timeout=10)
    wait(lambda: not client.get('/cam-sd.jpg/status/').json['live'], seconds=25)
    response = client.get('/cam-sd.jpg/')
    assert response.status_code == 200 and response.headers['X-DvorCam-Snapshot-State'] == 'fallback'
    assert hashlib.sha256(response.data).hexdigest() == saved_digest
    time.sleep(3)
    assert hashlib.sha256(client.get('/cam-sd.jpg/').data).hexdigest() == saved_digest
    print('PASS source loss -> persistent real frame; NO SIGNAL never overwrites it', flush=True)
    media.terminate()
    media.wait(timeout=10)
    shutil.rmtree(core.SNAPSHOTS)
    core.SNAPSHOTS.mkdir()
    assert client.get('/cam-sd.jpg/').data == saved.read_bytes()
    subprocess.run([sys.executable, str(root / 'app/bootstrap.py')], check=True,
                   env=os.environ.copy(), stdout=log, stderr=log)
    assert core.state() == value
    print('PASS restart without archive/RAM preserves camera IDs and last JPEG', flush=True)
    print('Evidence: ' + str(work), flush=True)
finally:
    for process in reversed(processes):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    log.close()
