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
   private-key blocks.
2. **Key / field name** — the enclosing map key, model field, or env-var name
   matches ``key|secret|token|password|credential`` (case-insensitive).

Both are intentionally broad; expect occasional false positives on
high-entropy-but-non-secret values. The visible marker and the opt-out make that
tolerable. This is a defense-in-depth aid, not a guarantee — callers that need
strong guarantees should still scrub on their own side.

Direct users of the Python functions (e.g. a wrapper service that owns its own
redaction layer) get raw, un-redacted return values and can call
:func:`redact_object` themselves if desired.
"""

import copy
import functools
import logging
import os
import re
from typing import Any

from pydantic import BaseModel

#: Marker substituted for any redacted value.
REDACTED = "[REDACTED]"

#: Map keys / model fields / env-var names whose *value* should be redacted.
_KEY_NAME_RE = re.compile(r"key|secret|token|password|passwd|credential|cred", re.IGNORECASE)

#: Value-shape patterns — a string value matching any of these is redacted
#: regardless of the key it lives under.
_VALUE_SHAPE_RES = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                              # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT / bearer token
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),       # PEM private key block
)


def _value_is_secret_shaped(s: str) -> bool:
    return any(rx.search(s) for rx in _VALUE_SHAPE_RES)


def _redact(value: Any, key_is_sensitive: bool = False) -> tuple[Any, int]:
    """Recursively redact ``value``, returning ``(redacted_value, count)``.

    Containers (dicts, lists, Pydantic models) are mutated in place and returned;
    strings are immutable so the parent assigns the replacement.
    """
    if isinstance(value, str):
        if value == REDACTED:
            return value, 0
        if key_is_sensitive or _value_is_secret_shaped(value):
            return REDACTED, 1
        return value, 0

    if isinstance(value, BaseModel):
        count = 0
        for name in type(value).model_fields:
            field_value = getattr(value, name)
            sensitive = bool(_KEY_NAME_RE.search(name))
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
            and bool(_KEY_NAME_RE.search(name_field))
        )
        for k, v in list(value.items()):
            sensitive = isinstance(k, str) and bool(_KEY_NAME_RE.search(k))
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
