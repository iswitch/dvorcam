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
                f'color=c={color}:size={size}:rate=4', '-t', '2', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-threads', '1', '-bf', '0', '-g', '4',
                '-f', 'h264', 'pipe:1'], capture_output=True, check=True)
            output.write(result.stdout)
    return stream, stages


@pytest.mark.parametrize('lose_storage', [False, True])
def test_snapshots_follow_current_frame_dimensions(installation, changing_stream, monkeypatch, lose_storage):
    core, web, worker = installation
    stream, stages = changing_stream
    target = core.ARCHIVE / '.snapshots/cam-sd.jpg'
    sizes = []
    replace = os.replace
    source = av.open(str(stream))
    clock = [0]

    def packets_with_clock(*args):
        for index, frame in enumerate(source.demux(video=0)):
            clock[0] = index / 4
            yield frame

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
        replace(temporary, destination)
        if lose_storage and len(sizes) == 3:
            (core.ARCHIVE / '.dvorcam-storage').unlink()

    with monkeypatch.context() as patch:
        patch.setattr(av, 'open', open_stream)
        patch.setattr(os, 'replace', publish)
        patch.setattr(sys, 'argv', ['snapshot.py', 'cam-sd'])
        patch.setattr(time, 'monotonic', lambda: clock[0])
        if lose_storage:
            with pytest.raises(SystemExit) as stopped:
                runpy.run_path(str(Path(core.__file__).with_name('snapshot.py')))
            assert stopped.value.code == 1
        else:
            runpy.run_path(str(Path(core.__file__).with_name('snapshot.py')))
    expected = [tuple(map(int, size.split('x'))) for size, _ in stages for _ in range(2)]
    assert sizes == (expected[:3] if lose_storage else expected)
    assert not list(target.parent.glob('*.tmp'))
    if not lose_storage:
        value = core.state()
        value['cameras'] = [{'id': 'cam', 'streams': {'sd': {}}}]
        core.atomic_json(core.DATA / 'state.json', value)
        client = web.app.test_client()
        for url in ('/cam-sd.jpg/',):
            assert client.get(url).data == target.read_bytes()
            assert client.head(url).status_code == 200
