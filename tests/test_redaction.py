"""Tests for the secret-redaction pass (k8stools.redaction)."""

import inspect
import json
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


# --- granularity: a match should not black out unrelated text ----------------
#
# Reported against 2.0.2: a ConfigMap holding config-as-JSON under one key came
# back as `{"warm-runtimes.json": "[REDACTED]"}` because one substring inside the
# blob matched a value-shape pattern. The whole file - warm-pool sizing, nothing
# secret - became unreadable. The same opacity cut the other way too: secrets
# named inside such a blob were invisible to the key-name rule entirely.

_JWT = "eyJhbGciOiJIUzI1Ni.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w"
_PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAsecretkeymaterial123456\n"
        "-----END RSA PRIVATE KEY-----")


def test_a_token_inside_a_plain_string_redacts_only_its_own_span():
    out, count = redact_object({"note": f"use {_JWT} to authenticate"})
    assert out["note"] == f"use {REDACTED} to authenticate"
    assert count == 1


def test_a_pem_block_is_still_redacted_in_full():
    """The PEM pattern matches only the header; the key material is what follows.
    Substituting just the matched span would black out the BEGIN line and publish
    the key, so a value containing a PEM header goes in its entirety."""
    out, count = redact_object({"blob": f"leading text\n{_PEM}\ntrailing text"})
    assert out["blob"] == REDACTED
    assert "MIIEowIBAAKCAQEAsecretkeymaterial123456" not in str(out)
    assert count == 1


def test_json_configmap_value_keeps_everything_but_the_match():
    """The reported case: one incidental token match must not hide the config."""
    import json
    blob = json.dumps({"target_count": 5, "runtimes": ["v1", "v2"],
                       "bootstrap": _JWT}, indent=2)
    out, count = redact_object({
        "name": "warm-runtimes-config", "namespace": "openhands-cloud-c3",
        "data": {"warm-runtimes.json": blob}, "binary_data_keys": []})
    redacted = json.loads(out["data"]["warm-runtimes.json"])
    assert redacted["target_count"] == 5          # the number they needed
    assert redacted["runtimes"] == ["v1", "v2"]
    assert redacted["bootstrap"] == REDACTED
    assert count == 1


def test_secrets_named_inside_a_json_blob_are_found():
    """The under-redaction half: before this, the key-name rule could not see
    into a JSON string, so these passed through verbatim."""
    import json
    blob = json.dumps({"db": {"password": "hunter2", "api_token": "abc"},
                       "host": "db.internal"})
    out, count = redact_object({"data": {"app.json": blob}})
    redacted = json.loads(out["data"]["app.json"])
    assert redacted["db"]["password"] == REDACTED
    assert redacted["db"]["api_token"] == REDACTED
    assert redacted["host"] == "db.internal"
    assert count == 2


def test_a_pem_nested_in_a_json_blob_goes_whole_but_takes_nothing_else():
    import json
    blob = json.dumps({"cert": "public-cert-data", "priv": _PEM, "port": 443})
    out, count = redact_object({"data": {"tls.json": blob}})
    redacted = json.loads(out["data"]["tls.json"])
    assert redacted["priv"] == REDACTED
    assert redacted["cert"] == "public-cert-data"
    assert redacted["port"] == 443
    assert count == 1


def test_the_key_exemption_holds_inside_a_json_blob_too():
    """Lifting the exemption in here looked right - these are the author's field
    names, not Kubernetes schema - but measured against a real 38-pod cluster it
    added 12 redactions, every one of them `tags[].key: "instance"` in a Grafana
    dashboard ConfigMap, and not one secret. Structural `key` fields dominate
    inside config documents just as they do in the API."""
    import json
    blob = json.dumps({"targets": [{"tags": [{"key": "instance", "value": "db-1"}]}]})
    out, count = redact_object({"data": {"dash.json": blob}})
    assert out["data"]["dash.json"] == blob   # untouched, not even re-serialized
    assert count == 0


def test_a_credential_shaped_value_under_key_is_still_caught_inside_a_blob():
    """What keeps the exemption affordable: the value-shape rules do not care
    what the field is called."""
    import json
    blob = json.dumps({"key": "AKIAIOSFODNN7EXAMPLE"})
    out, count = redact_object({"data": {"cfg.json": blob}})
    assert json.loads(out["data"]["cfg.json"])["key"] == REDACTED
    assert count == 1


def test_a_clean_json_value_is_left_byte_for_byte_alone():
    """No match means no re-serialization, so formatting is never churned."""
    original = '{\n    "count": 5,\n    "name": "pool"\n}'
    out, count = redact_object({"data": {"cfg.json": original}})
    assert out["data"]["cfg.json"] == original
    assert count == 0


@pytest.mark.parametrize("value", [
    "not json at all", "{broken json", "", "   ",
    "5", "true", "null", '"just a string"',
])
def test_non_container_json_values_are_untouched(value):
    """Bare scalars must not round-trip through the JSON path."""
    out, count = redact_object({"data": {"k": value}})
    assert out["data"]["k"] == value
    assert count == 0


def test_a_json_array_blob_is_also_recursed():
    import json
    blob = json.dumps([{"name": "a", "token": "s3kr1t"}, {"name": "b", "port": 80}])
    out, count = redact_object({"data": {"list.json": blob}})
    redacted = json.loads(out["data"]["list.json"])
    assert redacted[0]["token"] == REDACTED
    assert redacted[1]["port"] == 80
    assert count == 1


# --- credentials embedded in longer values (issue #12) -------------------------
#
# Neither token-shaped nor under a sensitive name, these are found by their
# context: URL userinfo, a sensitive flag or setting name. Only the credential
# goes; the rest stays readable. The negatives matter as much: each rule was
# measured against an unredacted capture of a real 38-pod cluster, where it
# added 3 redactions (one database password, in three formats) and no false
# positives. Several negatives below are the false positives that measurement
# found in earlier drafts.

def _r(value):
    return redact_object(value)[0]


@pytest.mark.parametrize("given,expected", [
    # The issue's table
    (["--db-url=postgres://admin:hunter2@db/x"], ["--db-url=postgres://admin:[REDACTED]@db/x"]),
    (["--password=hunter2"], ["--password=[REDACTED]"]),
    (["--token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"], ["--token", "[REDACTED]"]),
    (["-Dapi.key=AKIAIOSFODNN7EXAMPLE"], ["-Dapi.key=[REDACTED]"]),
    # URL credentials: the password only
    ("postgres://otelu:otelp@postgresql/otel?sslmode=disable",
     "postgres://otelu:[REDACTED]@postgresql/otel?sslmode=disable"),
    ("redis://:s3cret@cache:6379/0", "redis://:[REDACTED]@cache:6379/0"),
    # Flags and system properties
    (["-Dfoo.secret=abc", "-DsecretKey=x"], ["-Dfoo.secret=[REDACTED]", "-DsecretKey=[REDACTED]"]),
    ('app --password "two words" --x', "app --password [REDACTED] --x"),
    ("sh -c 'app --token abc123 --verbose'", "sh -c 'app --token [REDACTED] --verbose'"),
    ('JAVA_OPTS="-Xmx1g -Dpassword=hunter2 -Dx=1"', 'JAVA_OPTS="-Xmx1g -Dpassword=[REDACTED] -Dx=1"'),
    # Connection strings
    ("Host=postgresql;Username=otelu;Password=otelp;Database=otel",
     "Host=postgresql;Username=otelu;Password=[REDACTED];Database=otel"),
    ("Server=db;Uid=sa;Pwd=Hunter2;", "Server=db;Uid=sa;Pwd=[REDACTED];"),
    ("host=postgresql user=otelu password=otelp dbname=otel",
     "host=postgresql user=otelu password=[REDACTED] dbname=otel"),
    # Query strings, including presigned URLs' signatures
    ("https://api.example.com/v1?access_token=abc123&page=2",
     "https://api.example.com/v1?access_token=[REDACTED]&page=2"),
    ("https://b.s3.amazonaws.com/o?X-Amz-Date=20261007&X-Amz-Signature=deadbeef",
     "https://b.s3.amazonaws.com/o?X-Amz-Date=20261007&X-Amz-Signature=[REDACTED]"),
    ("https://acct.blob.core.windows.net/c/f?sv=2022&sig=AbC%2Fd&se=2026",
     "https://acct.blob.core.windows.net/c/f?sv=2022&sig=[REDACTED]&se=2026"),
    # Properties files in ConfigMaps
    ("spring.datasource.password=hunter2\nspring.datasource.url=jdbc:x",
     "spring.datasource.password=[REDACTED]\nspring.datasource.url=jdbc:x"),
])
def test_embedded_credentials_are_redacted_in_place(given, expected):
    assert _r(given) == expected


@pytest.mark.parametrize("value", [
    # The issue's negatives
    ["--log-level=debug"], "VALKEY_ADDR=valkey-cart:6379", "http://user@host/x",
    ["--keyspace=foo"],
    # A reference to a secret is not a secret
    ["--password-file=/etc/pw", "--secret-name", "db", "--api-key-env", "API_KEY"],
    "postgres://u:$(DB_PASSWORD)@db/x", ["--token", "$(TOKEN)"], "password=${DB_PASSWORD}",
    ["--tls-key", "/etc/certs/tls.key"], ["--key=/etc/tls.key"],
    # Not a credential
    ["--use-token=true"], "PWD=/home/app", "export PATH=/usr/bin",
    "https://registry.npmjs.org:443/@babel/core", "--token -v", 'msg="token refreshed" level=info',
    "password=****", "password=<your password>",
    # False positives found on a real cluster's logs (etcd config, metrics-server --help)
    "client-cert=, client-key=, trusted-ca = /var/lib/minikube/certs/etcd/ca.crt",
    "      --kubelet-client-key string                Path to a client key file for TLS.",
    "      --tls-sni-cert-key namedCertKey          A pair of x509 certificate and private key",
    "use the --tls-sni-cert-key multiple times.",
    "Selector (label query) to filter on (e.g. -l key1=value1,key2=value2).",
])
def test_things_that_look_like_settings_but_hold_no_credential(value):
    _, count = redact_object(value)
    assert count == 0, _r(value)


def test_running_text_needs_a_credential_like_value_but_argv_does_not():
    """Prose puts words after flags ("use --tls-sni-cert-key multiple times"),
    so in running text the value must have a digit or symbol, or be long. An
    argv list is unambiguous, so it doesn't."""
    assert _r("app --password letmein") == "app --password letmein"
    assert _r(["app", "--password", "letmein"]) == ["app", "--password", "[REDACTED]"]
    assert _r("app --password=letmein") == "app --password=[REDACTED]"


def test_rotated_args_in_a_workload_history_diff_are_redacted_on_both_sides():
    """get_workload_history renders changed args as a JSON string, so a rotated
    credential appears twice; redaction looks inside the JSON."""
    from k8stools.k8s_tools import TemplateChange
    change = TemplateChange(field="containers[app].args", change="changed",
                            before='["--token","ghp_abcdefghij0123456789"]',
                            after='["--token","ghp_klmnopqrst9876543210"]')
    redacted, count = redact_object(change)
    assert count == 2
    assert json.loads(redacted.before) == json.loads(redacted.after) == ["--token", "[REDACTED]"]


def test_a_secret_in_a_positional_argument_is_a_documented_limit():
    """Nothing names it, so it can't be told from any other argument."""
    assert _r(["app", "hunter2"]) == ["app", "hunter2"]


# --- false positives the #12 measurement found in the existing rules -----------
#
# On a real 38-pod cluster the name rule made 6 redactions, 3 of them not
# secrets. These pin each one, and the limits of the fixes.

def test_affinity_topology_keys_are_not_redacted_in_either_spelling():
    """get_pod_spec returns the client's snake_case, where the exact-`key`
    exemption never saw `topology_key` (2 of the 6)."""
    for name in ("topology_key", "topologyKey"):
        assert _r({name: "kubernetes.io/hostname"}) == {name: "kubernetes.io/hostname"}
    term = {"match_label_keys": ["pod-template-hash"],
            "mismatchLabelKeys": ["app.kubernetes.io/instance"]}
    assert redact_object(term)[1] == 0


def test_configmap_binary_entry_names_are_not_redacted():
    assert _r({"name": "x", "binary_data_keys": ["logo.png"]})["binary_data_keys"] == ["logo.png"]


def test_a_sensitive_name_does_not_make_a_boolean_or_reference_a_secret():
    """`gcp-auth-skip-secret: "true"` was the third."""
    for value in ("true", "False", "$(DB_PASSWORD)", "${token}", "$API_KEY",
                  "/etc/certs/tls.key", "{{ .Values.password }}", ""):
        assert _r({"password": value}) == {"password": value}, value


def test_a_password_that_starts_with_a_dollar_is_still_a_password():
    """References are recognised case-sensitively, like env var names."""
    for value in ("$Password123", "$ecret", "$2b$12$abcdefghijklmnopqrstuv"):
        assert _r({"password": value}) == {"password": REDACTED}, value


def test_compound_key_names_chosen_by_users_still_match():
    for name in ("api_key", "apiKey", "secret_key", "ssh_key", "SECRET_KEY_BASE"):
        assert _r({name: "abc123"}) == {name: REDACTED}, name
