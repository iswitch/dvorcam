"""Against tests/compose.smoke.yaml only: synthetic camera, no production sources."""
import base64
import http.cookiejar
import json
from io import BytesIO
from PIL import Image
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor

base = 'http://127.0.0.1:18880'
opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
opener.addheaders = [('Authorization', 'Basic ' + base64.b64encode(b'admin:local-smoke-only-password').decode())]


def fetch(path, data=None):
    request = Request(base + path, data=urlencode(data).encode() if data is not None else None)
    with opener.open(request, timeout=80) as response:
        return response.read()


if __name__ == '__main__':
    for _ in range(30):
        try:
            page = fetch('/admin/').decode()
            break
        except Exception:
            time.sleep(2)
    else:
        raise SystemExit('Gateway did not become ready')
    assert 'Камер пока нет' in page, 'Run on fresh smoke volumes only'
    # The camera form is a separate page; the empty list intentionally has no writable form.
    form_page = fetch('/admin/?new=1').decode()
    token = re.search(r'name="csrf" value="([^"]+)"', form_page)[1]
    page = fetch('/admin/camera', {'csrf': token, 'id': 'synthetic', 'name': 'Synthetic test camera',
                                 'rtsp_hd': 'rtsp://camera:8554/test', 'record': 'on'}).decode()
    if 'Synthetic test camera' not in page:
        raise SystemExit('Camera was not added: ' + re.sub('<[^>]*>', '', page)[:800])
    print('PASS: empty installation, admin authentication, camera probe and save', flush=True)
    time.sleep(18)
    jpeg = fetch('/synthetic-hd.jpg/')
    assert jpeg[:2] == b'\xff\xd8'
    print('PASS: live JPEG from the synthetic RTSP camera', flush=True)
    fetch('/admin/camera', {'csrf': token, 'id': 'synthetic', 'editing': 'synthetic', 'name': 'Synthetic test camera'})
    for _ in range(30):
        data = json.loads(fetch('/archive/synthetic/'))
        if data['clips']:
            break
        time.sleep(2)
    else:
        raise SystemExit('Closed recording was not published')
    assert data['playback_mode'] == 'static_segments'
    clip = data['clips'][-1]
    video = clip['url']
    with opener.open(Request(base + video, method='HEAD')) as response:
        assert response.status == 200 and int(response.headers['Content-Length']) == clip['size_bytes']
    with opener.open(Request(base + video, headers={'Range': 'bytes=0-99'})) as response:
        assert response.status == 206 and len(response.read()) == 100
    assert json.loads(fetch('/archive/synthetic/?' + urlencode({'at': clip['start'] + 1})))['selection']['offset_seconds'] == 1
    print('PASS: recording -> close -> verified MP4, metadata, seek offset, HEAD and HTTP Range through Caddy', flush=True)

    for _ in range(30):
        try:
            preview = fetch(clip['preview_url'])
            with Image.open(BytesIO(preview)) as image:
                image.load()
                assert image.size == (data['preview']['width'], data['preview']['height'])
                assert image.width == 240
            break
        except HTTPError as error:
            if error.code != 404:
                raise
            time.sleep(2)
    else:
        raise SystemExit('Archive preview was not restored')
    print('PASS: proportional archive preview available through Caddy', flush=True)
