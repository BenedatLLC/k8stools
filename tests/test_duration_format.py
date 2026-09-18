"""Tests that `timedelta` fields serialize to values their own schema accepts.

Pydantic describes a `timedelta` field as `{"type": "string", "format": "duration"}`
but serializes one carrying microseconds as `"P2DT5M27.978616S"`. JSON Schema's
`duration` format references RFC 3339 Appendix A, whose grammar admits only whole
numbers - so the default schema and the default serializer contradict each other,
and an MCP client that validates structured output against the declared schema
(ajv does) rejects the call outright, on every row, before the caller sees data.

Every age here is `now() - <k8s timestamp>`; Kubernetes timestamps are whole-second,
so the fractional part is always just the microseconds of that `now()` - which is
why this reproduced on essentially every row of every list tool against a live
cluster, while the largely whole-second mock fixture mostly hid it.

`k8s_tools.Duration` normalizes the value so the declared format stays truthful.
"""

import datetime
import inspect
import re
import typing

import pytest
from pydantic import BaseModel

from k8stools import k8s_tools, mock_tools


#: RFC 3339 Appendix A `duration`, the grammar JSON Schema's `format: "duration"`
#: points at. Transcribed from ajv-formats, which is what the reporting client used.
#: Note there is no decimal point anywhere in it, and no sign.
RFC3339_DURATION_RE = re.compile(
    r"^P(?!$)((\d+Y)?(\d+M)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?|(\d+W)?)$"
)

#: A duration with a sub-second component, as every live-cluster age has.
FRACTIONAL = datetime.timedelta(days=2, minutes=5, seconds=27, microseconds=978616)


def _duration_fields(model: type[BaseModel]) -> list[str]:
    """Field names whose *serialization* schema declares `format: "duration"`.

    Read off the generated schema rather than the annotation, so the test asserts
    against exactly what an MCP client is handed and told to validate against.
    """
    schema = model.model_json_schema(mode="serialization")
    names = []
    for name, prop in schema.get("properties", {}).items():
        branches = [prop, *prop.get("anyOf", [])]
        if any(b.get("format") == "duration" for b in branches):
            names.append(name)
    return names


def _models_with_durations() -> list[type[BaseModel]]:
    return [
        obj for obj in vars(k8s_tools).values()
        if inspect.isclass(obj) and issubclass(obj, BaseModel)
        and obj is not BaseModel and _duration_fields(obj)
    ]


def _accepts_none(annotation) -> bool:
    return type(None) in typing.get_args(annotation)


def _placeholder(annotation):
    """A minimal value satisfying `annotation`, for the fields we aren't testing."""
    if _accepts_none(annotation):
        return None
    origin = typing.get_origin(annotation) or annotation
    return {str: "x", int: 0, bool: False, float: 0.0, list: [], dict: {}}.get(origin)


def _build(model: type[BaseModel], value, overrides=None) -> BaseModel:
    """Instantiate `model`, putting `value` in every duration field.

    `overrides` replaces individual fields afterwards, for the cases where one
    duration field needs a different value than the rest.
    """
    durations = _duration_fields(model)
    kwargs = {}
    for name, field in model.model_fields.items():
        if name in durations:
            kwargs[name] = value
        elif field.is_required():
            kwargs[name] = _placeholder(field.annotation)
    kwargs.update(overrides or {})
    return model(**kwargs)


# --- the normalization itself -----------------------------------------------

@pytest.mark.parametrize("given,expected_seconds", [
    (datetime.timedelta(seconds=27, microseconds=978616), 27),
    (datetime.timedelta(microseconds=1), 0),
    (datetime.timedelta(0), 0),
    (datetime.timedelta(days=8), 8 * 86400),
    # A cluster clock ahead of ours yields a negative age, which pydantic renders
    # as "-PT5S" - no more valid under this grammar than a fractional one.
    (datetime.timedelta(seconds=-5), 0),
    (datetime.timedelta(days=-2), 0),
])
def test_durations_are_truncated_to_whole_nonnegative_seconds(given, expected_seconds):
    assert k8s_tools._to_whole_seconds(given) == datetime.timedelta(seconds=expected_seconds)


def test_field_keeps_a_real_timedelta_for_direct_python_callers():
    """Normalizing the value, not the type: `.age` is still a timedelta."""
    ns = k8s_tools.NamespaceSummary(name="d", status="Active", age=FRACTIONAL)
    assert isinstance(ns.age, datetime.timedelta)
    assert ns.age == datetime.timedelta(days=2, minutes=5, seconds=27)


def test_the_declared_schema_still_says_duration():
    """The fix must not paper over the problem by dropping the format annotation."""
    prop = k8s_tools.NamespaceSummary.model_json_schema()["properties"]["age"]
    assert prop == {"title": "Age", "type": "string", "format": "duration"}


# --- every model, against its own declared schema ---------------------------

def test_there_are_duration_fields_to_check():
    """Guard against the sweeps below silently degrading to no-ops."""
    models = _models_with_durations()
    assert len(models) >= 10
    assert k8s_tools.PodSummary in models


@pytest.mark.parametrize("model", _models_with_durations(), ids=lambda m: m.__name__)
def test_fractional_input_serializes_within_the_declared_format(model):
    dumped = _build(model, FRACTIONAL).model_dump(mode="json")
    for name in _duration_fields(model):
        assert RFC3339_DURATION_RE.match(dumped[name]), \
            f"{model.__name__}.{name} = {dumped[name]!r} violates format: duration"


@pytest.mark.parametrize("model", _models_with_durations(), ids=lambda m: m.__name__)
def test_optional_duration_fields_still_accept_none(model):
    """Normalization must not turn an absent duration into PT0S."""
    optional = [n for n in _duration_fields(model)
                if _accepts_none(model.model_fields[n].annotation)]
    if not optional:
        pytest.skip(f"{model.__name__} has no optional duration field")
    dumped = _build(model, FRACTIONAL, {n: None for n in optional}).model_dump(mode="json")
    for name in optional:
        assert dumped[name] is None


# --- end to end, over the actual tool surface -------------------------------

@pytest.mark.parametrize("tool", mock_tools.TOOLS, ids=lambda f: f.__name__)
def test_every_tool_emits_conformant_durations(tool):
    """The end-to-end shape of the bug report: call the tool, validate every
    duration in its structured output against the format its schema declares."""
    mock_tools.load_mock_state()
    params = inspect.signature(tool).parameters
    if any(p.default is inspect.Parameter.empty for p in params.values()):
        pytest.skip(f"{tool.__name__} needs arguments")
    result = tool()

    items = result if isinstance(result, list) else [result]
    for item in items:
        if not isinstance(item, BaseModel):
            continue
        dumped = item.model_dump(mode="json")
        for name in _duration_fields(type(item)):
            if dumped[name] is None:
                continue
            assert RFC3339_DURATION_RE.match(dumped[name]), \
                f"{tool.__name__} -> {type(item).__name__}.{name} = " \
                f"{dumped[name]!r} violates format: duration"


def test_mock_namespaces_were_the_reproducing_case():
    """`get_namespaces` is the no-argument reproducer from the report, and the one
    place the mock fixture did carry fractional seconds (`P1Y55DT1H37M32.49238S`)."""
    mock_tools.load_mock_state()
    ages = [ns.model_dump(mode="json")["age"] for ns in mock_tools.get_namespaces()]
    assert ages, "fixture should have namespaces"
    for age in ages:
        assert RFC3339_DURATION_RE.match(age), f"{age!r} violates format: duration"
