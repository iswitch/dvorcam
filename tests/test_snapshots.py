import os
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
import runpy
import subprocess
import sys
import time

import av
from PIL import Image
import pytest


@pytest.fixture
def changing_stream(tmp_path):
    # One H.264 connection: placeholder, landscape, portrait, another width, landscape again.
    stages = [('640x480', 'black'), ('640x360', 'red'), ('480x640', 'green'),
              ('800x600', 'blue'), ('640x360', 'red')]
    stream = tmp_path / 'changing.h264'
    with stream.open('wb') as output:
        for size, color in stages:
            result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                f'color=c={color}:size={size}:rate=4', '-t', '6', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-threads', '1', '-bf', '0', '-g', '4',
                '-f', 'h264', 'pipe:1'], capture_output=True, check=True)
            output.write(result.stdout)
    return stream, stages


@pytest.mark.parametrize('lose_storage', [False, True])
def test_snapshots_follow_current_frame_dimensions(installation, changing_stream, monkeypatch, lose_storage):
    core, web, worker = installation
    stream, stages = changing_stream
    target = core.SNAPSHOTS / 'cam-sd.jpg'
    sizes = []
    published_at = []
    replace = os.replace
    source = av.open(str(stream))
    clock = [0]
    decode_calls = []

    class Packet:
        def __init__(self, packet):
            self.packet = packet
            self.is_keyframe = packet.is_keyframe

        def decode(self):
            decode_calls.append(clock[0])
            return self.packet.decode()

    def packets_with_clock(*args):
        for index, packet in enumerate(source.demux(video=0)):
            clock[0] = index / 4
            yield Packet(packet)

    @contextmanager
    def open_stream(*args, **kwargs):
        with source:
            yield SimpleNamespace(streams=source.streams, demux=packets_with_clock)

    def publish(temporary, destination):
        if destination != target:
            return replace(temporary, destination)
        # The old JPEG remains complete until an equally complete replacement is published.
        if target.exists():
            with Image.open(target) as previous:
                previous.load()
        with Image.open(temporary) as current:
            current.load()
            sizes.append(current.size)
            published_at.append(clock[0])
        replace(temporary, destination)
        if lose_storage and len(sizes) == 3:
            (core.ARCHIVE / '.dvorcam-storage').unlink()

    with monkeypatch.context() as patch:
        patch.setattr(av, 'open', open_stream)
        patch.setattr(os, 'replace', publish)
        patch.setattr(sys, 'argv', ['snapshot.py', 'cam-sd'])
        patch.setattr(time, 'monotonic', lambda: clock[0])
        runpy.run_path(str(Path(core.__file__).with_name('snapshot.py')))
    expected = {tuple(map(int, size.split('x'))) for size, _ in stages}
    assert set(sizes) == expected
    assert len(published_at) >= 25
    assert all(1 <= current - previous <= 1.25
               for previous, current in zip(published_at, published_at[1:]))
    assert (core.LAST_FRAMES / 'cam-sd.jpg').exists()
    assert all(current - previous >= 1 for previous, current in zip(decode_calls, decode_calls[1:]))
    assert not list(target.parent.glob('*.tmp'))
    if not lose_storage:
        value = core.state()
        value['cameras'] = [{'id': 'cam', 'streams': {'sd': {}}}]
        core.atomic_json(core.DATA / 'state.json', value)
        client = web.app.test_client()
        for url in ('/cam-sd.jpg/',):
            assert client.get(url).data == target.read_bytes()
            assert client.head(url).status_code == 200


@pytest.mark.parametrize('disk_failure', [False, True])
def test_disk_fallback_throttled_and_failure_keeps_live(installation, monkeypatch, disk_failure):
    core, _, _ = installation
    clock, live_writes, saves = [0], [], []
    image = Image.new('RGB', (64, 48), 'red')
    packet = SimpleNamespace(is_keyframe=True, decode=lambda: [SimpleNamespace(to_image=lambda: image.copy())])
    def demux(*args):
        for second in range(96):
            clock[0] = second
            yield packet
    codec = SimpleNamespace(thread_count=0, skip_frame='')
    @contextmanager
    def open_stream(*args, **kwargs):
        yield SimpleNamespace(streams=SimpleNamespace(video=[SimpleNamespace(codec_context=codec)]), demux=demux)
    real_replace = os.replace
    def replace(source, destination):
        if destination == core.SNAPSHOTS / 'cam-sd.jpg':
            live_writes.append(clock[0])
        if destination == core.LAST_FRAMES / 'cam-sd.jpg':
            saves.append(clock[0])
            if disk_failure:
                raise OSError('failed system fallback write')
        return real_replace(source, destination)
    monkeypatch.setattr(av, 'open', open_stream)
    monkeypatch.setattr(os, 'replace', replace)
    monkeypatch.setattr(sys, 'argv', ['snapshot.py', 'cam-sd'])
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    script = str(Path(core.__file__).with_name('snapshot.py'))
    runpy.run_path(script)
    assert len(live_writes) >= 90
    assert len(saves) == 4 and all(b - a >= 30 for a, b in zip(saves, saves[1:]))
    if not disk_failure:
        # Restart immediately: the persisted timestamp prevents another immediate SSD write.
        saves.clear()
        clock[0] = 0
        runpy.run_path(script)
        assert saves[0] >= 30
    assert codec.skip_frame == 'NONKEY' and codec.thread_count == 1
