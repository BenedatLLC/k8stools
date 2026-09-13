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


# --- whole-word name matching (rule D) --------------------------------------

@pytest.mark.parametrize("name", [
    "AWS_SECRET_ACCESS_KEY", "apiKey", "privateKey", "accessToken", "x-auth-token",
    "DB_PASSWORD", "credentials", "MY_CRED", "passwd", "Secret",
    "keys", "tokens", "imagePullSecrets",   # plurals are still names of secrets
])
def test_sensitive_names_still_match(name):
    out, count = redact_object({name: "s3kr1t"})
    assert out[name] == REDACTED, f"{name} should be treated as sensitive"
    assert count == 1


@pytest.mark.parametrize("name", [
    "VALKEY_ADDR",      # a hostname; `key` is a substring of `valkey`
    "valkey-cart",      # a container name - this one cost a whole captured log
    "monkey_patch",
    "TURNKEY_MODE",
    "hockey",
])
def test_names_that_merely_contain_a_sensitive_substring_do_not_match(name):
    out, count = redact_object({name: "valkey-cart:6379"})
    assert out[name] == "valkey-cart:6379", f"{name} is not a secret-bearing name"
    assert count == 0


def test_a_containers_logs_are_not_redacted_by_its_name():
    logs = "1:M 12 Sep 2026 22:38:47.039 * Background saving started by pid 263"
    out, count = redact_object({"valkey-cart": logs})
    assert out["valkey-cart"] == logs
    assert count == 0


# --- structural `key` exemption (rule A) ------------------------------------

def test_toleration_keys_are_not_redacted():
    # 68 of 139 redactions on a real cluster were toleration keys.
    tolerations = [{"key": "node.kubernetes.io/not-ready", "operator": "Exists",
                    "effect": "NoExecute", "toleration_seconds": 300}]
    out, count = redact_object({"tolerations": tolerations})
    assert out["tolerations"][0]["key"] == "node.kubernetes.io/not-ready"
    assert count == 0


def test_projected_configmap_item_keys_are_not_redacted():
    # These are filenames (`ca.crt`), not credentials.
    vol = {"projected": {"sources": [{"config_map": {
        "name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]}}
    out, count = redact_object(vol)
    assert out["projected"]["sources"][0]["config_map"]["items"][0]["key"] == "ca.crt"
    assert count == 0


def test_label_selector_and_topology_keys_are_not_redacted():
    term = {"label_selector": {"match_expressions": [
                {"key": "app", "operator": "In", "values": ["ad"]}]}}
    out, count = redact_object(term)
    assert out["label_selector"]["match_expressions"][0]["key"] == "app"
    assert count == 0


def test_a_reference_to_a_secret_is_not_a_secret():
    """`secretKeyRef` is a pointer: the value lives in the Secret and is resolved
    by the kubelet, never appearing in tool output. Redacting the pointer hides
    which key feeds an env var and protects nothing."""
    env = {"name": "DB_PASS",
           "value_from": {"secret_key_ref": {"name": "db-creds", "key": "admin-password"}}}
    out, count = redact_object(env)
    assert out["value_from"]["secret_key_ref"]["key"] == "admin-password"
    assert out["value_from"]["secret_key_ref"]["name"] == "db-creds"
    assert count == 0


def test_the_key_exemption_is_case_sensitive_and_exact():
    # Kubernetes schema fields are the lowercase `key`; anything else is a name
    # someone chose, and stays in the net.
    out, _ = redact_object({"KEY": "s3kr1t", "api_key": "s3kr1t", "key": "app"})
    assert out["KEY"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["key"] == "app"


# --- env-var names keep the full rule ---------------------------------------

def test_env_var_values_are_still_redacted_by_their_name():
    env = [{"name": "SECRET_KEY_BASE", "value": "yYrECL4qbNwleYInGJYvVnSkwJuSQJ4i"},
           {"name": "POSTGRES_PASSWORD", "value": "otel"},
           {"name": "OPENAI_API_KEY", "value": "dummy"}]
    out, count = redact_object({"env": env})
    assert [e["value"] for e in out["env"]] == [REDACTED] * 3
    assert count == 3


def test_an_env_var_named_exactly_key_is_still_redacted():
    # The structural exemption covers schema fields, not variables a user named.
    out, count = redact_object({"env": [{"name": "KEY", "value": "s3kr1t"}]})
    assert out["env"][0]["value"] == REDACTED
    assert count == 1


def test_env_var_with_a_substring_match_is_left_alone():
    out, count = redact_object({"env": [{"name": "VALKEY_ADDR", "value": "valkey-cart:6379"}]})
    assert out["env"][0]["value"] == "valkey-cart:6379"
    assert count == 0


def test_value_shape_still_wins_regardless_of_name():
    # The shape rules produced zero false positives on a real cluster; they stay
    # in force under any field name, including exempted ones.
    out, count = redact_object({"key": "AKIAIOSFODNN7EXAMPLE", "hockey": "AKIAIOSFODNN7EXAMPLE"})
    assert out["key"] == REDACTED
    assert out["hockey"] == REDACTED
    assert count == 2
