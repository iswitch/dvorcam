from copy import deepcopy
from test_archive import AUTH


def test_group_rename_preserves_cameras_and_archive(installation):
    core, web, _ = installation
    value = core.state()
    value['cameras'] = [{'id': str(i), 'name': 'Camera', 'group': group, 'record': True,
                        'streams': {'hd': {'rtsp': 'rtsp://camera/live'}}}
                       for i, group in enumerate(('Двор', 'двор', 'Подъезды', ''))]
    core.atomic_json(core.DATA / 'state.json', value)
    index = core.ARCHIVE / '.ready' / '0' / 'index.json'
    index.parent.mkdir(); index.write_text('[]')
    client = web.app.test_client()
    assert client.post('/admin/group/rename').status_code == 401
    client.get('/admin/', headers=AUTH)
    with client.session_transaction() as session:
        csrf = session['csrf']
    assert client.post('/admin/group/rename', headers=AUTH, data={'old_group': 'Двор', 'name': 'Сад'}).status_code == 403
    before = deepcopy(core.state())
    for name in ('', ' ', 'x' * 81, 'Подъезды'):
        response = client.post('/admin/group/rename', headers=AUTH, data={'csrf': csrf, 'old_group': 'Двор', 'name': name})
        assert 'error=' in response.location
        assert core.state() == before
    response = client.post('/admin/group/rename', headers=AUTH, data={'csrf': csrf, 'old_group': 'Двор', 'name': ' Сад '})
    assert 'message=' in response.location
    before['cameras'][0]['group'] = before['cameras'][1]['group'] = 'Сад'
    assert core.state() == before
    assert index.read_text() == '[]'
    assert 'Сад' in client.get('/admin/', headers=AUTH).text
    response = client.post('/admin/group/rename', headers=AUTH, data={'csrf': csrf, 'old_group': 'Двор', 'name': 'Другой'})
    assert 'error=' in response.location
    assert core.state() == before
