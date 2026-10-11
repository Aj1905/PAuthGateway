"""Offline regression checks: synthetic requests/responses, no live services."""
import io
import socket
import urllib.error
import urllib.request

import pytest

from gateway.providers import openapi_suite as api


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network access is forbidden in these tests')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


@pytest.mark.parametrize('target', [
    'https://other.example.invalid/collect',
    'http://api.example.invalid/collect',
    'https://api.example.invalid:444/collect',
])
def test_redirect_cannot_carry_credentials_to_another_origin(target):
    request = urllib.request.Request('https://api.example.invalid/start', headers={
        'Authorization': 'Bearer SYNTHETIC', 'X-Api-Key': 'SYNTHETIC',
        'Cookie': 'synthetic=only'})
    with pytest.raises(api.OpenAPIError, match='cross-origin'):
        api._ValidatingRedirectHandler().redirect_request(
            request, None, 302, 'Found', {}, target)


@pytest.mark.parametrize('target', [
    'https://api.example.invalid/next',
    'https://API.EXAMPLE.INVALID:443/next',
])
def test_same_origin_redirect_preserves_authentication(target):
    request = urllib.request.Request('https://api.example.invalid/start', headers={
        'Authorization': 'Bearer SYNTHETIC'})
    redirected = api._ValidatingRedirectHandler().redirect_request(
        request, None, 302, 'Found', {}, target)
    assert redirected.get_header('Authorization') == 'Bearer SYNTHETIC'


@pytest.mark.parametrize('host', [
    '169.254.169.254', '0251.0376.0251.0376', '169.254.43518',
    '2852039166', '0xA9FEA9FE', '0xa9.0xfe.0xa9.0xfe',
    '[fe80::1]', '[::ffff:169.254.169.254]',
])
def test_metadata_host_spellings_are_rejected_without_dns(host):
    with pytest.raises(api.OpenAPIError, match='link-local'):
        api._require_http_url('http://' + host + '/synthetic', 'offline')


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8080/api', 'http://10.0.0.1/api',
    'http://[::1]/api', 'https://api.example.invalid/api',
])
def test_intended_internal_and_dns_backends_remain_allowed(url):
    api._require_http_url(url, 'offline')


class Body(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []
    def read(self, *args):
        self.read_sizes.append(args)
        return super().read(*args)


def execute_with_opener(monkeypatch, opener):
    monkeypatch.setattr(api, '_SAFE_OPENER', opener)
    env = api._OpenAPIEnv('https://api.example.invalid', {}, {})
    operation = api._Operation('test', 'GET', '/synthetic', ())
    return api._execute_operation(env, operation, {})


@pytest.mark.parametrize('oversized', [False, True])
def test_http_error_body_is_bounded_and_closed(monkeypatch, oversized):
    # Lower the cap: exercise the real branch with at most 33 synthetic bytes.
    monkeypatch.setattr(api, '_MAX_RESPONSE_BYTES', 32)
    body = Body(b'x' * (33 if oversized else 8))
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://api.example.invalid', 500,
                                         'synthetic', {}, body)
    expected = 'response exceeds size cap' if oversized else 'HTTP 500'
    with pytest.raises(api.OpenAPIError, match=expected):
        execute_with_opener(monkeypatch, Opener())
    assert body.read_sizes == [(33,)]
    assert body.closed


def test_successful_json_response_remains_supported(monkeypatch):
    body = Body(b'{"ok":true}')
    body.headers = {'Content-Type': 'application/json'}
    class Opener:
        def open(self, *args, **kwargs):
            return body
    assert execute_with_opener(monkeypatch, Opener()) == {'ok': True}
    assert body.read_sizes == [(api._MAX_RESPONSE_BYTES + 1,)]
    assert body.closed
