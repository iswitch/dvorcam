import pytest


@pytest.mark.parametrize('flag,address', [(None, '127.0.0.1:8554'), ('false', '127.0.0.1:8554'), ('true', '0.0.0.0:8554')])
def test_rtsp_opt_in_retains_internal_services_and_permissions(installation, monkeypatch, flag, address):
    core, _, _ = installation
    if flag is None:
        monkeypatch.delenv('RTSP_DOCKER_ACCESS', raising=False)
    else:
        monkeypatch.setenv('RTSP_DOCKER_ACCESS', flag)
    config = core.media_config(core.state())
    assert config['rtspAddress'] == address
    assert config['rtspTransports'] == ['tcp']
    assert config['apiAddress'] == '127.0.0.1:9997'
    readers, internal = config['authInternalUsers']
    assert readers['permissions'] == [{'action': 'read', 'path': '~^[A-Za-z0-9_-]+-(hd|sd)$'}]
    assert internal['ips'] == ['127.0.0.1', '::1']
    assert internal['permissions'] == [{'action': 'api'}]


@pytest.mark.parametrize('flag', ['', 'yes', 'tru'])
def test_rtsp_mistyped_access_flag_fails_closed(installation, monkeypatch, flag):
    core, _, _ = installation
    monkeypatch.setenv('RTSP_DOCKER_ACCESS', flag)
    with pytest.raises(ValueError, match='RTSP_DOCKER_ACCESS'):
        core.media_config(core.state())
