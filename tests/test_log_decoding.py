"""Tests for log decoding at the Kubernetes API boundary (issue #6).

`read_namespaced_pod_log` is the only tool call whose response is a bare scalar
rather than a model, and the generated client stringifies its body with
`str(data)`. Since kubernetes 36 that body is `bytes`, so the client returns
`repr(bytes)`: one `b'...'` line with newlines as the two characters `\\n`.
`get_logs_for_pod_and_container` therefore asks for the raw response and decodes
it itself; these tests pin that down from both directions, plus the redaction and
`previous`-plumbing behavior that depends on it.
"""

from types import SimpleNamespace

import pytest

from k8stools import k8s_tools
from k8stools.redaction import REDACTED, redact_object, wrap_with_redaction


class FakeLogApi:
    """Stands in for CoreV1Api, returning whatever body the test hands it."""

    def __init__(self, body):
        self.body = body
        self.last_log_kwargs = None

    def read_namespaced_pod_log(self, **kwargs):
        self.last_log_kwargs = kwargs
        if isinstance(self.body, (bytes, str)) or self.body is None:
            # The raw urllib3 response carries the body on `.data`.
            return SimpleNamespace(data=self.body)
        return self.body


@pytest.fixture
def fake_api(monkeypatch):
    def install(body):
        api = FakeLogApi(body)
        monkeypatch.setattr(k8s_tools, "K8S", api)
        return api
    return install


# --- decoding ---------------------------------------------------------------

LOG_BYTES = (b"2026-09-19T00:00:00Z line one\n"
             b"2026-09-19T00:00:01Z line two\n"
             b"2026-09-19T00:00:02Z line three\n")


def test_bytes_body_decoded_to_text(fake_api):
    fake_api(LOG_BYTES)
    logs = k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c")
    assert isinstance(logs, str)
    assert not logs.startswith("b'")
    assert logs.count("\n") == 3          # real newlines...
    assert "\\n" not in logs              # ...not the two characters of an escape
    assert logs.splitlines()[0].endswith("line one")


def test_str_body_passed_through_not_double_decoded(fake_api):
    """A client that decodes for us (or a preloaded response) must not be re-decoded."""
    fake_api("2026-09-19T00:00:00Z already text\n")
    logs = k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c")
    assert logs == "2026-09-19T00:00:00Z already text\n"


def test_bare_str_response_without_data_attribute(monkeypatch):
    """A preloaded response is a bare str with no `.data` at all; still fine."""
    class PreloadedApi:
        def read_namespaced_pod_log(self, **kwargs):
            return "plain string response"

    monkeypatch.setattr(k8s_tools, "K8S", PreloadedApi())
    assert k8s_tools.get_logs_for_pod_and_container("pod-1") == "plain string response"


def test_invalid_utf8_does_not_raise(fake_api):
    """`limit_bytes` truncates at a byte offset, which can split a character."""
    fake_api(b"2026-09-19T00:00:00Z caf\xe9 \xf0\x9f\x92 truncated\n")
    logs = k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c")
    assert "�" in logs
    assert "truncated" in logs


@pytest.mark.parametrize("body", [b"", None])
def test_empty_log_is_empty_string(fake_api, body):
    fake_api(body)
    assert k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c") == ''


def test_raw_response_requested(fake_api):
    """Flipping `_preload_content` back on reintroduces `repr(bytes)`; pin it off."""
    api = fake_api(LOG_BYTES)
    k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c")
    assert api.last_log_kwargs["_preload_content"] is False


# --- redaction --------------------------------------------------------------

SECRET_LOG = (b"2026-09-19T00:00:00Z starting up\n"
              b"2026-09-19T00:00:01Z using key AKIAIOSFODNN7EXAMPLE\n")


@pytest.mark.parametrize("body", [SECRET_LOG, SECRET_LOG.decode()])
def test_secret_in_logs_is_redacted(fake_api, body):
    """Logs are the largest and most secret-prone payload the server returns;
    they must not escape the redaction boundary in either response shape."""
    fake_api(body)
    tool = wrap_with_redaction(k8s_tools.get_logs_for_pod_and_container)
    logs = tool("pod-1", "default", "c")
    assert "AKIAIOSFODNN7EXAMPLE" not in logs
    assert REDACTED in logs
    assert "starting up" in logs          # span-granular: the rest stays readable


def test_bytes_are_redacted_not_passed_through():
    """Defence in depth: bytes reaching `_redact` directly are handled, not
    dropped into the never-secret-shaped scalar branch."""
    out, count = redact_object({"blob": b"token AKIAIOSFODNN7EXAMPLE here"})
    assert count == 1
    assert isinstance(out["blob"], bytes)
    assert b"AKIAIOSFODNN7EXAMPLE" not in out["blob"]
    assert REDACTED.encode() in out["blob"]


def test_clean_bytes_keep_their_exact_value():
    original = b"nothing secret here\n\xff\xfe"
    out, count = redact_object({"blob": original})
    assert count == 0
    assert out["blob"] == original        # not re-encoded, so no lossy round-trip


# --- `previous` plumbing ----------------------------------------------------

def test_previous_forwarded_once(fake_api):
    api = fake_api(LOG_BYTES)
    k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c", previous=True)
    assert api.last_log_kwargs["previous"] is True


def test_previous_false_omits_the_key(fake_api):
    """The API rejects `previous` for a container with no terminated instance, so
    the key is left out entirely rather than sent as False."""
    api = fake_api(LOG_BYTES)
    k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "c")
    assert "previous" not in api.last_log_kwargs
