# Copyright (c) 2025 Benedat LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
#
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Secret redaction for tool output.

Some read-only Kubernetes resources can legitimately carry secret-shaped values
even though they are not ``Secret`` objects: ``ConfigMap`` ``data`` maps and the
``env`` blocks returned by :func:`k8stools.k8s_tools.get_pod_spec` are the common
cases. When k8stools' MCP server is pointed directly at an agent (no wrapping
service in front to scrub output), those values would otherwise flow verbatim
into the model's context and possibly its logs.

This module provides a single redaction pass applied at the MCP server's output
boundary (see ``mcp_server.py``). It is **on by default** and can be disabled
with the ``--no-redact`` flag or ``K8STOOLS_REDACT=0``. Redaction never drops a
field; it replaces the matched value with the visible marker ``[REDACTED]`` so an
agent can distinguish "absent" from "hidden", and the number of redactions is
logged for auditability.

Two independent signals trigger a redaction:

1. **Value shape** — the string looks like a credential regardless of where it
   sits: AWS access keys (``AKIA...``), bearer/JWT tokens (``eyJ...``), or PEM
   private-key blocks. A self-contained token is replaced where it sits, leaving
   the rest of the surrounding text readable; a PEM header stands for a secret
   that continues past the match, so that value goes in full.
2. **Key / field name** — the enclosing map key, model field, or env-var name
   contains one of ``key``, ``secret``, ``token``, ``password``, ``passwd``,
   ``credential``, ``cred`` **as a whole word**. Names are split on separators and
   on camelCase boundaries, so ``AWS_SECRET_ACCESS_KEY``, ``apiKey`` and
   ``x-auth-token`` all match, while ``VALKEY_ADDR`` does not.

   A field named *exactly* ``key`` is exempt, because in the Kubernetes API that
   always identifies a map entry, a taint, a label selector, or an item in a
   projected volume — never a credential. Measured on a real 38-pod cluster, this
   one exemption accounted for 130 of 139 redactions, none of them secrets:
   toleration keys (``node.kubernetes.io/not-ready``), projected ConfigMap
   filenames (``ca.crt``), and ``secretKeyRef.key``. That last one is worth
   spelling out: a *reference* to a secret is not a secret — the value lives in
   the Secret object and is resolved by the kubelet, never appearing in any tool
   output — so redacting the pointer hides which key feeds an env var and protects
   nothing. The exemption does not apply to env-var names, where a variable a user
   named ``KEY`` plausibly does hold one.

A string value that is itself a JSON object or array — config-as-JSON under a
single ConfigMap key is a common pattern — is parsed and redacted entry by entry
rather than treated as one opaque blob, then re-serialized (which normalizes its
whitespace). Both rules apply inside, so an incidental token match no longer
blacks out a whole config file and a ``{"password": ...}`` buried in one no longer
escapes the name rule. Inside such a blob the ``key`` exemption is lifted: those
names are the author's, not Kubernetes schema.

Expect occasional false positives on high-entropy-but-non-secret values. The
visible marker and the opt-out make that tolerable. This is a defense-in-depth
aid, not a guarantee — callers that need strong guarantees should still scrub on
their own side.

Direct users of the Python functions (e.g. a wrapper service that owns its own
redaction layer) get raw, un-redacted return values and can call
:func:`redact_object` themselves if desired.
"""

import copy
import functools
import json
import logging
import os
import re
from typing import Any, Optional

from pydantic import BaseModel

#: Marker substituted for any redacted value.
REDACTED = "[REDACTED]"

#: Words that, when they appear in a map key / model field / env-var name, mark
#: that entry's *value* as sensitive.
_SENSITIVE_WORDS = frozenset({
    "key", "secret", "token", "password", "passwd", "credential", "cred",
})

#: Splits an identifier into words on separators *and* camelCase boundaries, so
#: `AWS_SECRET_ACCESS_KEY`, `apiKey` and `x-auth-token` all decompose correctly.
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")

#: Field names that are structural Kubernetes schema, never a secret. In the
#: Kubernetes API a field named exactly `key` always identifies a map entry, a
#: taint, a label selector or an item in a projected volume - never a credential.
#: Real secrets are named in compounds (`apiKey`, `SECRET_KEY_BASE`). Matched
#: case-sensitively; see `_field_name_is_sensitive`.
_STRUCTURAL_FIELD_NAMES = frozenset({"key"})


def _name_words(name: str) -> list[str]:
    """Lowercased words of an identifier, split on separators and camelCase."""
    words = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        words.extend(match.group(0).lower() for match in _WORD_RE.finditer(part))
    return words


def _name_is_sensitive(name: str) -> bool:
    """Whether a name marks its value sensitive, by whole word rather than substring.

    Matching on whole words rather than substrings is what keeps `VALKEY_ADDR`
    (a hostname) and a container named `valkey-cart` out of the net - `key` is a
    substring of `valkey`, and a substring match redacted both.
    """
    return any(_word_is_sensitive(word) for word in _name_words(name))


def _word_is_sensitive(word: str) -> bool:
    """A sensitive word, or its plural - `credentials`, `keys`, `tokens` all count."""
    if word in _SENSITIVE_WORDS:
        return True
    return word.endswith("s") and word[:-1] in _SENSITIVE_WORDS


def _field_name_is_sensitive(name: str) -> bool:
    """As :func:`_name_is_sensitive`, but exempting structural schema field names.

    Applies to map keys and model fields, which in a Kubernetes payload are mostly
    schema, not user-chosen names. Not applied to env-var names: an env var a user
    named `KEY` plausibly does hold one, whereas `tolerations[].key` never does.
    """
    # Case-sensitive: every structural field in the Kubernetes schema is the
    # lowercase `key`, so a shouty `KEY` is a name someone chose, not schema, and
    # stays in the net.
    if name.strip() in _STRUCTURAL_FIELD_NAMES:
        return False
    return _name_is_sensitive(name)

#: Value-shape patterns whose match *is* the whole secret. Because the match is
#: self-contained, only the matched span needs to go — the surrounding text is
#: ordinary config and stays readable.
_TOKEN_SHAPE_RES = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                              # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT / bearer token
)

#: Value-shape patterns that merely *mark* a secret whose sensitive part lies
#: outside the match. A PEM header is the whole of the match but none of the
#: secret - the key material is the base64 body that follows - so a value matching
#: one of these is replaced in full. Substituting only the matched span here would
#: black out the `-----BEGIN RSA PRIVATE KEY-----` line and publish the key.
_CONTAINMENT_SHAPE_RES = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),       # PEM private key block
)


def _value_is_secret_shaped(s: str) -> bool:
    """Whether ``s`` matches any value-shape pattern, anywhere in the string."""
    return any(
        rx.search(s) for rx in (*_TOKEN_SHAPE_RES, *_CONTAINMENT_SHAPE_RES)
    )


def _redact_json_string(s: str) -> Optional[tuple[str, int]]:
    """Redact inside a string that is itself a JSON document.

    Storing config-as-JSON under a single ConfigMap key is a common pattern, and
    it defeats a redactor that treats every string as one opaque value in *both*
    directions: one incidental token match blacks out an entire config file, while
    a real ``{"password": "..."}`` inside the blob is invisible to the key-name
    rule and passes through verbatim. Parsing, recursing, and re-serializing fixes
    both — a match redacts its own entry and nothing else.

    Returns ``None`` when ``s`` is not a JSON object/array, or when recursion found
    nothing, so the caller falls through and the original text is left byte-for-byte
    intact. Re-serialization otherwise normalizes whitespace, which is why it only
    happens when there is actually something to hide.
    """
    stripped = s.strip()
    # Cheap gate: only objects and arrays are worth parsing. It also keeps bare
    # scalars ("5", "true") from round-tripping through json.dumps.
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    # The `key` exemption applies in here too. Lifting it looked right - these are
    # the author's field names, not Kubernetes schema - but measured against a real
    # cluster it added 12 redactions, all of them `tags[].key: "instance"` in a
    # Grafana dashboard ConfigMap, and no secrets. Structural `key` fields dominate
    # inside config documents exactly as they do in the API. A credential-shaped
    # value under a `key` field is still caught by the value-shape rules.
    redacted, count = _redact(parsed)
    if not count:
        return None
    return json.dumps(redacted, indent=2 if "\n" in s else None), count


def _redact_string(value: str, key_is_sensitive: bool) -> tuple[str, int]:
    """Redact a single string value. See :func:`_redact` for the parameters."""
    if value == REDACTED:
        return value, 0
    if key_is_sensitive:
        return REDACTED, 1

    # Before treating the string as opaque text, see whether it is structured. A
    # PEM block nested inside a JSON blob is handled by this recursion too: it
    # arrives back here as a leaf and hits the containment rule below.
    nested = _redact_json_string(value)
    if nested is not None:
        return nested

    if any(rx.search(value) for rx in _CONTAINMENT_SHAPE_RES):
        return REDACTED, 1

    count = 0
    for rx in _TOKEN_SHAPE_RES:
        value, n = rx.subn(REDACTED, value)
        count += n
    return value, count


def _redact(value: Any, key_is_sensitive: bool = False) -> tuple[Any, int]:
    """Recursively redact ``value``, returning ``(redacted_value, count)``.

    Containers (dicts, lists, Pydantic models) are mutated in place and returned;
    strings are immutable so the parent assigns the replacement.
    """
    if isinstance(value, str):
        return _redact_string(value, key_is_sensitive)

    if isinstance(value, BaseModel):
        count = 0
        for name in type(value).model_fields:
            field_value = getattr(value, name)
            sensitive = _field_name_is_sensitive(name)
            new_value, c = _redact(field_value, key_is_sensitive=sensitive)
            if c:
                setattr(value, name, new_value)
            count += c
        return value, count

    if isinstance(value, dict):
        count = 0
        # Kubernetes env vars serialize as {"name": <ENV_NAME>, "value": <val>};
        # the sensitivity lives in the env-var *name*, not a dict key, so special-case it.
        name_field = value.get("name")
        env_value_sensitive = (
            isinstance(name_field, str)
            and "value" in value
            and _name_is_sensitive(name_field)
        )
        for k, v in list(value.items()):
            sensitive = isinstance(k, str) and _field_name_is_sensitive(k)
            if env_value_sensitive and k == "value":
                sensitive = True
            new_value, c = _redact(v, key_is_sensitive=sensitive)
            if c:
                value[k] = new_value
            count += c
        return value, count

    if isinstance(value, list):
        count = 0
        for i, item in enumerate(value):
            new_value, c = _redact(item)
            if c:
                value[i] = new_value
            count += c
        return value, count

    # int / float / bool / None / datetime / timedelta / etc. — never secret-shaped.
    return value, 0


def redact_object(obj: Any) -> tuple[Any, int]:
    """Return a redacted deep copy of ``obj`` and the number of values redacted.

    The input is never mutated. Works on Pydantic models, dicts, lists, and any
    nesting of them (the return types of every k8stools tool).
    """
    clone = copy.deepcopy(obj)
    return _redact(clone)


def redaction_enabled(no_redact_flag: bool = False) -> bool:
    """Decide whether redaction should be applied.

    Redaction is on by default. It is disabled if ``no_redact_flag`` is set or if
    the ``K8STOOLS_REDACT`` environment variable is one of ``0/false/no/off``
    (case-insensitive).
    """
    if no_redact_flag:
        return False
    env = os.environ.get("K8STOOLS_REDACT")
    if env is not None and env.strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def wrap_with_redaction(fn):
    """Wrap a tool function so its return value passes through :func:`redact_object`.

    The wrapper preserves ``fn``'s signature, annotations, and docstring (via
    :func:`functools.wraps`), so MCP tool-schema generation is unaffected.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        redacted, count = redact_object(result)
        if count:
            logging.info(f"redaction: {fn.__name__} redacted {count} secret-shaped value(s)")
        return redacted
    return wrapper
