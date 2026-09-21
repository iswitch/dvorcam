"""Run on fresh volumes with compose.smoke.yaml + compose.variants.yaml."""
import json
import os
import re
import subprocess
import time
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import urlopen
from PIL import Image
from smoke import fetch, base

project = os.environ.get('DVORCAM_TEST_PROJECT', 'dvorcam-smoke')
container = project + '-dvorcam-1'


def inside(code):
    return subprocess.check_output(['docker', 'exec', container, 'python', '-c', code], text=True)


def paths():
    items = json.loads(inside("from urllib.request import urlopen; print(urlopen('http://127.0.0.1:9997/v3/config/paths/list').read().decode())"))['items']
    return [item for item in items if not item['name'].endswith('-source')]


def wait_for(check, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (HTTPError, KeyError, IndexError, json.JSONDecodeError, subprocess.CalledProcessError):
            pass
        time.sleep(.5)
    raise AssertionError('Timed out waiting for variant state')


def jpeg_size(quality):
    with urlopen(base + '/synthetic-' + quality + '.jpg/', timeout=5) as response:
        with Image.open(BytesIO(response.read())) as image:
            image.load()
            return image.size


def last_video_size():
    return json.loads(inside("import json,subprocess; from core import entries,ARCHIVE; row=entries('synthetic')[-1]; print(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=width,height,codec_type','-of','json',str(ARCHIVE/'.ready/synthetic'/(row['segment_id']+'.mp4'))],text=True))"))['streams']


wait_for(lambda: bool(fetch('/healthz')))
page = fetch('/admin/').decode()
assert 'Камер пока нет' in page
form_page = fetch('/admin/?new=1').decode()
token = re.search(r'name="csrf" value="([^"]+)"', form_page)[1]
form = {'csrf': token, 'id': 'synthetic', 'editing': 'synthetic', 'name': 'Synthetic camera', 'group': 'Courtyard'}
fetch('/admin/camera', dict(form, rtsp_hd='rtsp://camera:8554/test', rtsp_sd='rtsp://camera:8554/sd', record='on'))
wait_for(lambda: {p['name']: p['record'] for p in paths()} == {'synthetic-hd': True, 'synthetic-sd': False})
assert {p['name']: p['record'] for p in paths()} == {'synthetic-hd': True, 'synthetic-sd': False}
configured = json.loads(inside("from urllib.request import urlopen; print(urlopen('http://127.0.0.1:9997/v3/config/paths/list').read().decode())"))['items']
assert {item['name'] for item in configured} == {
    'synthetic-hd', 'synthetic-sd', 'synthetic-hd-source', 'synthetic-sd-source'}
commands = inside("from pathlib import Path; print('\\n'.join(p.read_bytes().replace(b'\\0', b' ').decode(errors='replace') for p in Path('/proc').glob('[0-9]*/cmdline') if p.is_file()))")
assert 'rtsp://camera:8554/' not in commands
assert 'rtsp://127.0.0.1:8554/synthetic-hd-source' in commands
assert 'rtsp://127.0.0.1:8554/synthetic-sd-source' in commands
wait_for(lambda: jpeg_size('hd') == (1280, 720) and jpeg_size('sd') == (640, 480))
for quality in ('hd', 'sd'):
    assert b'<video' in urlopen(base + '/synthetic-' + quality + '/').read()
for path in ('/snapshots/synthetic.jpg', '/synthetic.jpg', '/synthetic/', '/api/v1/cameras/synthetic/archive'):
    try:
        urlopen(base + path)
    except HTTPError as error:
        assert error.code == 404
    else:
        raise AssertionError('Old URL still available: ' + path)
# A 10-second GOP must not make snapshots vanish between keyframes.
for _ in range(24):
    assert jpeg_size('sd') == (640, 480)
    time.sleep(.5)
print('PASS: public HD/SD players and JPEG, long GOP freshness, old URLs removed', flush=True)
subprocess.run(['docker', 'stop', project + '-feed-1'], check=True, stdout=subprocess.DEVNULL)
try:
    wait_for(lambda: json.loads(inside("from urllib.request import urlopen; print(urlopen('http://127.0.0.1:9997/v3/paths/get/synthetic-hd').read().decode())"))['online'] is False)
    time.sleep(5)
    assert {p['name']: p['record'] for p in paths()} == {'synthetic-hd': True, 'synthetic-sd': False}
    assert inside("from pathlib import Path; print(len(list(Path('/archive/synthetic-sd').glob('*.mp4'))))").strip() == '0'
    wait_for(lambda: last_video_size() == [{'codec_type': 'video', 'width': 1280, 'height': 720}])
    print('PASS: HD loss does not start SD recording; HD archive contains no audio', flush=True)
    fetch('/admin/camera', dict(form, remove_hd='yes', record='on'))
    wait_for(lambda: len(paths()) == 1 and paths()[0]['name'] == 'synthetic-sd' and paths()[0]['record'])
    for path in ('/synthetic-hd/', '/synthetic-hd.jpg/'):
        try:
            urlopen(base + path)
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError('Removed HD variant is still available')
    time.sleep(16)
    fetch('/admin/camera', form)
    wait_for(lambda: last_video_size() == [{'codec_type': 'video', 'width': 640, 'height': 480}])
    print('PASS: removing HD selects SD for the same camera archive; MP4 is video-only', flush=True)
finally:
    subprocess.run(['docker', 'start', project + '-feed-1'], check=True, stdout=subprocess.DEVNULL)
time.sleep(3)
fetch('/admin/camera', dict(form, rtsp_hd='rtsp://camera:8554/test'))
wait_for(lambda: len(paths()) == 2)
assert 'Courtyard' in fetch('/admin/').decode()
print('PASS: both variants restored, group retained', flush=True)
