from __future__ import annotations

from dataclasses import MISSING, dataclass, fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Callable, Literal, get_args, get_origin


@dataclass(frozen=True)
class ActionContext:
    connector_root: Path


@dataclass(frozen=True)
class ActionSpec:
    type: str
    payload: type
    enrich: Callable[[dict[str, Any], Any, ActionContext], dict[str, Any]] | None = None
    hazard: Literal["safe", "write", "danger"] = "write"
    description: str = ""


class ActionRegistry:
    _specs: dict[str, ActionSpec] = {}

    @classmethod
    def register(cls, spec: ActionSpec) -> None:
        if not isinstance(spec.type, str) or not spec.type.strip():
            raise ValueError("Action type must be a non-empty string")
        if not is_dataclass(spec.payload):
            raise TypeError(f"Action payload for '{spec.type}' must be a dataclass")
        if spec.type in cls._specs:
            raise ValueError(f"Action type already registered: {spec.type}")
        cls._specs[spec.type] = spec

    @classmethod
    def get(cls, action_type: str) -> ActionSpec | None:
        """Alias for get_spec — returns None if the type is not registered."""
        return cls.get_spec(action_type)

    @classmethod
    def known_types(cls) -> list[str]:
        return sorted(cls._specs.keys())

    @classmethod
    def get_spec(cls, action_type: str) -> ActionSpec | None:
        return cls._specs.get(action_type)

    @classmethod
    def validate(cls, action: dict[str, Any]) -> str | None:
        try:
            cls._validate_and_build(action)
            return None
        except ValueError as exc:
            return str(exc)

    @classmethod
    def enrich(cls, actions: list[dict[str, Any]], context: ActionContext) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for action in actions:
            spec, payload_obj = cls._validate_and_build(action)
            if spec is None:
                # Caller should validate before enrich; keep fail-closed anyway.
                raise ValueError(f"unknown action type '{action.get('type', '')}'")
            normalized = dict(action)
            if spec.enrich is not None:
                normalized = spec.enrich(normalized, payload_obj, context)
            out.append(normalized)
        return out

    @classmethod
    def describe(cls) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for action_type in cls.known_types():
            spec = cls._specs[action_type]
            rows.append(
                {
                    "type": spec.type,
                    "hazard": spec.hazard,
                    "description": spec.description,
                    "schema": _payload_schema(spec.payload),
                }
            )
        return rows

    @classmethod
    def _validate_and_build(cls, action: dict[str, Any]) -> tuple[ActionSpec, Any]:
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        action_type = action.get("type")
        if not isinstance(action_type, str) or not action_type.strip():
            raise ValueError("action missing 'type'")
        spec = cls._specs.get(action_type)
        if spec is None:
            raise ValueError(f"unknown action type '{action_type}'")
        payload_data = {k: v for k, v in action.items() if k != "type"}
        payload_obj = _build_payload(spec.payload, payload_data)
        return spec, payload_obj


def _build_payload(payload_cls: type, payload_data: dict[str, Any]) -> Any:
    payload_fields = fields(payload_cls)
    names = {f.name for f in payload_fields}
    extra = sorted(set(payload_data.keys()) - names)
    if extra:
        raise ValueError(f"unexpected payload field(s): {', '.join(extra)}")

    for f in payload_fields:
        has_default = f.default is not MISSING or f.default_factory is not MISSING
        if f.name not in payload_data:
            if not has_default:
                raise ValueError(f"missing required payload field '{f.name}'")
            continue
        value = payload_data[f.name]
        if not _matches_type(value, f.type):
            got = type(value).__name__
            want = _type_label(f.type)
            raise ValueError(f"payload field '{f.name}' expects {want}, got {got}")

    try:
        return payload_cls(**payload_data)
    except TypeError as exc:
        raise ValueError(f"invalid payload: {exc}") from exc


def _matches_type(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is None:
        if annotation is float:
            return isinstance(value, (float, int))
        if annotation is bool:
            return isinstance(value, bool)
        if annotation is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if annotation is str:
            return isinstance(value, str)
        if annotation is dict:
            return isinstance(value, dict)
        if annotation is list:
            return isinstance(value, list)
        if annotation is tuple:
            return isinstance(value, tuple)
        if isinstance(annotation, type):
            return isinstance(value, annotation)
        return True

    if origin in (list,):
        return isinstance(value, list)
    if origin in (dict,):
        return isinstance(value, dict)
    if origin in (tuple,):
        return isinstance(value, tuple)
    if origin is Literal:
        return value in args
    if origin in (UnionType,):
        return any(_matches_type(value, arg) for arg in args)
    if str(origin) == "typing.Union":
        return any(_matches_type(value, arg) for arg in args)
    return True


def _type_label(annotation: Any) -> str:
    if annotation is Any:
        return "any"
    origin = get_origin(annotation)
    if origin is Literal:
        vals = ", ".join(repr(v) for v in get_args(annotation))
        return f"literal({vals})"
    if origin is not None:
        return str(annotation).replace("typing.", "")
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _payload_schema(payload_cls: type) -> dict[str, Any]:
    required: list[str] = []
    properties: dict[str, Any] = {}
    for f in fields(payload_cls):
        has_default = f.default is not MISSING or f.default_factory is not MISSING
        if not has_default:
            required.append(f.name)
        properties[f.name] = {"type": _type_label(f.type)}
    return {"required": required, "properties": properties}
