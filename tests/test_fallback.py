import json
import runpy
import subprocess
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from PIL import ImageChops, ImageStat

from fallback import REVISION, generate_fallback
from test_archive import AUTH


@pytest.mark.parametrize('width,height,profile,rate,expected_profile', [
    (640, 360, 'Main', '25/1', 'Main'),
    (640, 480, 'Constrained Baseline', '20/1', 'Constrained Baseline'),
    (1920, 1088, 'Main', '20/1', 'Main'),
    (1920, 1088, 'Main', '239/12', 'Main'),
    (1280, 720, 'High', '30000/1001', 'High'),
    (480, 640, 'Baseline', '25/1', 'Constrained Baseline'),
    (3840, 2160, 'High', '60/1', 'High'),
])
def test_real_camera_profiles_and_animation(tmp_path, width, height, profile, rate, expected_profile):
    target = tmp_path / 'placeholder.mp4'
    generate_fallback({'width': width, 'height': height, 'profile': profile, 'r_frame_rate': rate}, target)
    metadata = json.loads(subprocess.run([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(target)
    ], capture_output=True, check=True).stdout)
    assert len(metadata['streams']) == 1
    video = metadata['streams'][0]
    assert (video['width'], video['height']) == (width, height)
    assert video['codec_name'] == 'h264' and video['profile'] == expected_profile
    assert video['has_b_frames'] == 0 and video['pix_fmt'] == 'yuv420p'
    assert video['sample_aspect_ratio'] == '1:1'
    assert Fraction(video['r_frame_rate']) == Fraction(rate)
    assert abs(float(metadata['format']['duration']) - 2) <= 1 / float(Fraction(rate))
    assert target.stat().st_mode & 0o777 == 0o600
    assert not target.with_suffix('.tmp.mp4').exists()
    # Decode across a loop boundary, including its final packet.
    subprocess.run(['ffmpeg', '-v', 'error', '-stream_loop', '1', '-i', str(target), '-f', 'null', '-'],
                   capture_output=True, check=True, timeout=30)
    with av.open(str(target)) as container:
        frames = [frame.to_image().resize((640, 360)) for frame in container.decode(video=0)]
    crop = (240, 100, 400, 200)
    first = frames[0].crop(crop)
    moving = frames[round(float(Fraction(rate)) / 2)].crop(crop)
    stationary = frames[-1].crop(crop)
    assert sum(ImageStat.Stat(ImageChops.difference(first, moving)).mean) > 1
    # Lossy H.264 quantization can differ between I/P frames; bound each RGB channel.
    assert max(ImageStat.Stat(ImageChops.difference(first, stationary)).mean) < 2


def test_save_camera_creates_real_animated_fallback(installation, monkeypatch):
    core, web, _ = installation
    original = subprocess.run
    video = {'codec_name': 'h264', 'codec_type': 'video', 'width': 640, 'height': 480,
             'profile': 'Main', 'r_frame_rate': '25/1', 'has_b_frames': 0}

    def probe(args, **kwargs):
        if args[0] == 'ffprobe' and '-rtsp_transport' in args:
            return SimpleNamespace(stdout=json.dumps({'streams': [video]}))
        return original(args, **kwargs)

    monkeypatch.setattr(web.subprocess, 'run', probe)
    client = web.app.test_client()
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    response = client.post('/admin/camera', headers=AUTH, data={
        'csrf': csrf, 'id': 'demo', 'rtsp_sd': 'rtsp://192.0.2.1/live', 'record': 'on'})
    assert 'message=' in response.location
    stream = core.state()['cameras'][0]['streams']['sd']
    assert stream['fallback_revision'] == REVISION
    target = core.DATA / 'fallback' / stream['fallback_file']
    assert target.is_file()
    with av.open(str(target)) as container:
        assert len(list(container.decode(video=0))) == 50
    config = core.media_config(core.state())['paths']['demo-sd']
    assert config['alwaysAvailableFile'] == str(target)
    assert config['alwaysAvailableRecorded'] is False
    assert 'runOnOnline' in config and 'runOnAvailable' not in config


@pytest.mark.parametrize('failure', [False, True])
def test_bootstrap_updates_offline_camera_once_and_keeps_old_fallback(installation, monkeypatch, failure):
    core, _, _ = installation
    import fallback
    old = core.DATA / 'fallback' / 'old.mp4'
    old.write_bytes(b'previous fallback')
    value = core.state()
    value['cameras'] = [{'id': 'demo', 'name': 'Offline', 'group': 'Yard', 'record': True,
                        'retention_days': 7, 'streams': {'sd': {
                            'rtsp': 'rtsp://192.0.2.1/offline', 'has_audio': False, 'fallback_file': old.name,
                            'video': {'width': 320, 'height': 180, 'r_frame_rate': '10/1', 'profile': 'Main'}}}}]
    core.atomic_json(core.DATA / 'state.json', value)
    before = (core.DATA / 'state.json').read_bytes()
    monkeypatch.setattr(core, 'apply_media', lambda *args, **kwargs: None)
    real_generate = fallback.generate_fallback
    calls = []

    def generate(video, target):
        calls.append(target)
        if failure:
            raise subprocess.TimeoutExpired('ffmpeg', 45)
        real_generate(video, target)

    monkeypatch.setattr(fallback, 'generate_fallback', generate)
    script = str(Path(core.__file__).with_name('bootstrap.py'))
    runpy.run_path(script)
    updated = core.state()
    current = updated['cameras'][0]['streams']['sd']
    assert old.read_bytes() == b'previous fallback'
    assert current['rtsp'] == value['cameras'][0]['streams']['sd']['rtsp']
    if failure:
        assert (core.DATA / 'state.json').read_bytes() == before
        assert list((core.DATA / 'fallback').iterdir()) == [old]
    else:
        assert current['fallback_revision'] == REVISION
        assert current['fallback_file'] != old.name
        assert (core.DATA / 'fallback' / current['fallback_file']).is_file()
        preserved = dict(updated['cameras'][0], streams=value['cameras'][0]['streams'])
        assert preserved == value['cameras'][0]
    stamp = (core.DATA / 'state.json').stat().st_mtime_ns
    runpy.run_path(script)
    assert len(calls) == (2 if failure else 1)
    assert (core.DATA / 'state.json').stat().st_mtime_ns == stamp


def test_encoding_failure_keeps_published_file_and_removes_temporary(tmp_path, monkeypatch):
    target = tmp_path / 'existing.mp4'
    target.write_bytes(b'previous')

    def fail(args, **kwargs):
        Path(args[-1]).write_bytes(b'incomplete')
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        generate_fallback({'width': 640, 'height': 360, 'r_frame_rate': '25/1'}, target)
    assert target.read_bytes() == b'previous'
    assert not target.with_suffix('.tmp.mp4').exists()
