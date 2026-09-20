import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setenv('DVORCAM_DATA', str(tmp_path / 'data'))
    monkeypatch.setenv('DVORCAM_ARCHIVE', str(tmp_path / 'archive'))
    monkeypatch.setenv('PUBLIC_URL', 'http://localhost')
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'a-test-password-not-production')
    import core
    importlib.reload(core)
    for path in (core.DATA, core.DATA / 'runtime', core.DATA / 'fallback', core.ARCHIVE,
                 core.ARCHIVE / '.ready', core.ARCHIVE / '.previews', core.ARCHIVE / '.snapshots'):
        path.mkdir(parents=True, exist_ok=True)
    (core.DATA / 'storage-id').write_text('test-storage')
    (core.ARCHIVE / '.dvorcam-storage').write_text('test-storage')
    core.atomic_json(core.DATA / 'state.json', {'version': 2, 'cameras': [], 'settings': dict(core.DEFAULT_SETTINGS)})
    import previews
    importlib.reload(previews)
    import web
    import worker
    importlib.reload(web)
    importlib.reload(worker)
    web.app.config['TESTING'] = True
    return core, web, worker
