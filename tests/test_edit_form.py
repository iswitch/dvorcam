import html
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_archive import AUTH


@pytest.mark.parametrize('change', ['none', 'password', 'username', 'address', 'clear_credentials', 'pasted_url'])
def test_prefilled_camera_edit(installation, monkeypatch, change):
    core, web, _ = installation
    stored = 'rtsp://user%40home:test%26%3C%3E%3A%40@[2001:db8::1]:554/live?channel=1&subtype=0'
    stream = {'rtsp': stored, 'has_audio': False, 'fallback_file': 'existing.mp4',
              'video': {'codec_type': 'video', 'codec_name': 'h264', 'width': 640, 'height': 480, 'r_frame_rate': '25/1'}}
    value = core.state()
    value['cameras'] = [{'id': 'demo', 'name': 'Camera', 'group': 'Yard', 'record': True, 'streams': {'hd': stream, 'sd': dict(stream)}}]
    core.atomic_json(core.DATA / 'state.json', value)
    client = web.app.test_client()
    assert client.get('/admin/?edit=demo').status_code == 401
    listing = client.get('/admin/', headers=AUTH).text
    assert 'test&amp;' not in listing and '2001:db8' not in listing
    page = client.get('/admin/?edit=demo', headers=AUTH).text
    fields = {}
    for tag in re.findall(r'<input\b[^>]*>', page):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
        if 'name' in attrs:
            fields[attrs['name']] = html.unescape(attrs.get('value', ''))
    fields['record'] = 'on'
    for quality in ('hd', 'sd'):
        assert fields['rtsp_' + quality] == 'rtsp://[2001:db8::1]:554/live?channel=1&subtype=0'
        assert fields['camera_user_' + quality] == 'user@home'
        assert fields['camera_password_' + quality] == 'test&<>:@'
        fields.pop('remove_' + quality, None)
    assert 'Оставьте пустым' not in page
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'ffprobe':
            return SimpleNamespace(stdout=json.dumps({'streams': [stream['video']]}))
        Path(args[-1]).write_bytes(b'fallback')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(web.subprocess, 'run', run)
    if change == 'password':fields['camera_password_hd'] = 'updated:@'
    if change == 'username':fields['camera_user_hd'] = 'new@user'
    if change == 'address':fields['rtsp_hd'] = 'rtsp://192.0.2.1/new'
    if change == 'clear_credentials':fields['camera_user_hd'] = fields['camera_password_hd'] = ''
    if change == 'pasted_url':fields['rtsp_hd'] = 'rtsp://pasted:new@192.0.2.1/new'
    response = client.post('/admin/camera', data=fields, headers=AUTH)
    assert 'message=' in response.location
    current = core.state()['cameras'][0]
    assert current['streams']['sd'] == stream
    if change == 'none':
        assert current == value['cameras'][0]
        assert calls == []
    else:
        assert len(calls) == 2
        url = current['streams']['hd']['rtsp']
        if change == 'password':assert 'updated%3A%40@' in url
        if change == 'username':assert 'new%40user:' in url
        if change == 'address':assert url.endswith('@192.0.2.1/new')
        if change == 'clear_credentials':assert '@' not in url
        if change == 'pasted_url':assert url == 'rtsp://pasted:new@192.0.2.1/new'
    before = core.state()
    fields['rtsp_hd'] = ''
    response = client.post('/admin/camera', data=fields, headers=AUTH)
    assert 'error=' in response.location
    assert core.state() == before
