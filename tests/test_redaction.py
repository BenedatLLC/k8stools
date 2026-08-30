"""Tests for the secret-redaction pass (k8stools.redaction)."""

import inspect
from typing import Optional

import pytest
from pydantic import BaseModel

from k8stools import redaction
from k8stools.redaction import REDACTED, redact_object, redaction_enabled, wrap_with_redaction


# --- value-shape matching ---------------------------------------------------

def test_aws_access_key_redacted():
    obj = {"note": "creds", "id": "AKIAIOSFODNN7EXAMPLE"}
    out, count = redact_object(obj)
    assert out["id"] == REDACTED
    assert out["note"] == "creds"
    assert count == 1


def test_jwt_redacted():
    token = "eyJhbGciOiJIUzI1Ni.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w"
    out, count = redact_object({"auth": token})
    assert out["auth"] == REDACTED
    assert count == 1


def test_pem_private_key_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc...\n-----END RSA PRIVATE KEY-----"
    out, count = redact_object({"blob": pem})
    assert out["blob"] == REDACTED
    assert count == 1


def test_non_secret_value_preserved():
    obj = {"LOG_LEVEL": "info", "replicas": "3", "host": "db.internal"}
    out, count = redact_object(obj)
    assert out == obj
    assert count == 0


# --- key / field name matching ----------------------------------------------

@pytest.mark.parametrize("key", ["password", "API_TOKEN", "db_secret", "credential", "signing-key"])
def test_sensitive_key_name_redacts_value(key):
    out, count = redact_object({key: "whatever-value"})
    assert out[key] == REDACTED
    assert count == 1


def test_env_var_name_triggers_value_redaction():
    # Kubernetes env serializes as {"name": <ENV>, "value": <val>}
    env = [
        {"name": "DB_PASSWORD", "value": "hunter2"},
        {"name": "LOG_LEVEL", "value": "info"},
    ]
    out, count = redact_object({"env": env})
    assert out["env"][0]["value"] == REDACTED
    assert out["env"][0]["name"] == "DB_PASSWORD"   # name is preserved
    assert out["env"][1]["value"] == "info"          # non-sensitive untouched
    assert count == 1


# --- nested structures / pydantic models ------------------------------------

class _Model(BaseModel):
    token: Optional[str] = None
    note: str = ""
    nested: dict = {}


def test_pydantic_model_field_redacted():
    m = _Model(token="s3cr3t", note="keep me", nested={"AWS_SECRET_ACCESS_KEY": "abc123"})
    out, count = redact_object(m)
    assert out.token == REDACTED           # field name matches
    assert out.note == "keep me"
    assert out.nested["AWS_SECRET_ACCESS_KEY"] == REDACTED
    assert count == 2


def test_input_is_not_mutated():
    original = {"password": "keepme", "env": [{"name": "TOKEN", "value": "abc"}]}
    out, count = redact_object(original)
    # deep copy means the original is untouched
    assert original["password"] == "keepme"
    assert original["env"][0]["value"] == "abc"
    assert out["password"] == REDACTED
    assert count == 2


def test_already_redacted_not_double_counted():
    out, count = redact_object({"password": REDACTED})
    assert out["password"] == REDACTED
    assert count == 0


# --- enable/disable ---------------------------------------------------------

def test_redaction_enabled_default(monkeypatch):
    monkeypatch.delenv("K8STOOLS_REDACT", raising=False)
    assert redaction_enabled() is True


def test_redaction_disabled_by_flag():
    assert redaction_enabled(no_redact_flag=True) is False


@pytest.mark.parametrize("val", ["0", "false", "no", "OFF"])
def test_redaction_disabled_by_env(monkeypatch, val):
    monkeypatch.setenv("K8STOOLS_REDACT", val)
    assert redaction_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes"])
def test_redaction_enabled_by_env(monkeypatch, val):
    monkeypatch.setenv("K8STOOLS_REDACT", val)
    assert redaction_enabled() is True


# --- wrapper ----------------------------------------------------------------

def test_wrap_preserves_signature_and_redacts():
    def sample(name: str, namespace: str = "default") -> dict:
        return {"name": name, "password": "leaked"}

    wrapped = wrap_with_redaction(sample)
    assert wrapped.__name__ == "sample"
    assert inspect.signature(wrapped) == inspect.signature(sample)
    result = wrapped("x")
    assert result["password"] == REDACTED
    assert result["name"] == "x"
