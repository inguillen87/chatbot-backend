"""Compile and validate the supported Meta WhatsApp Flow JSON 7.3 profile.

The public API deliberately separates a conceptual :class:`FlowBlueprint` from
an uploadable :class:`FlowJsonArtifact`.  This module only builds the Flow JSON
asset; endpoint transport, encryption, persistence, and publication are out of
scope.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit


FLOW_JSON_VERSION = "7.3"
DATA_API_VERSION = "3.0"
MAX_FLOW_JSON_BYTES = 10 * 1024 * 1024
MAX_SCREENS = 100
MAX_ROUTING_BRANCHES = 10

ALLOWED_ACTIONS = frozenset(
    {"navigate", "data_exchange", "complete", "open_url", "update_data"}
)
ACTION_PROPERTIES = frozenset(
    {"on-click-action", "on-select-action", "on-unselect-action"}
)
SUPPORTED_COMPONENTS = frozenset(
    {
        "CalendarPicker",
        "CheckboxGroup",
        "ChipsSelector",
        "DatePicker",
        "DocumentPicker",
        "Dropdown",
        "EmbeddedLink",
        "Footer",
        "Form",
        "If",
        "Image",
        "ImageCarousel",
        "NavigationList",
        "OptIn",
        "PhotoPicker",
        "RadioButtonsGroup",
        "RichText",
        "Switch",
        "TextArea",
        "TextBody",
        "TextCaption",
        "TextHeading",
        "TextInput",
        "TextSubheading",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {"version", "data_api_version", "routing_model", "screens"}
)
_SCREEN_KEYS = frozenset(
    {
        "id",
        "terminal",
        "success",
        "title",
        "refresh_on_back",
        "sensitive",
        "data",
        "layout",
    }
)
_ACTION_KEYS = {
    "navigate": frozenset({"name", "next", "payload"}),
    "data_exchange": frozenset({"name", "payload"}),
    "complete": frozenset({"name", "payload"}),
    "open_url": frozenset({"name", "url"}),
    "update_data": frozenset({"name", "payload"}),
}
_FORM_VALUE_COMPONENTS = frozenset(
    {
        "CalendarPicker",
        "CheckboxGroup",
        "ChipsSelector",
        "DatePicker",
        "DocumentPicker",
        "Dropdown",
        "NavigationList",
        "OptIn",
        "PhotoPicker",
        "RadioButtonsGroup",
        "TextArea",
        "TextInput",
    }
)
_COMPONENT_REQUIRED_PROPERTIES = {
    "CalendarPicker": ("name",),
    "CheckboxGroup": ("name", "label", "data-source"),
    "ChipsSelector": ("name", "label", "data-source"),
    "DatePicker": ("name", "label"),
    "DocumentPicker": ("name", "label"),
    "Dropdown": ("name", "label", "data-source"),
    "EmbeddedLink": ("text", "on-click-action"),
    "Footer": ("label", "on-click-action"),
    "Form": ("name", "children"),
    "If": ("condition", "then"),
    "Image": ("src",),
    "ImageCarousel": ("images",),
    "NavigationList": ("name",),
    "OptIn": ("name", "label"),
    "PhotoPicker": ("name", "label"),
    "RadioButtonsGroup": ("name", "label", "data-source"),
    "RichText": ("text",),
    "Switch": ("value", "cases"),
    "TextArea": ("name", "label"),
    "TextBody": ("text",),
    "TextCaption": ("text",),
    "TextHeading": ("text",),
    "TextInput": ("name", "label"),
    "TextSubheading": ("text",),
}
_SCHEMA_TYPES = frozenset({"string", "number", "boolean", "object", "array"})
_SCHEMA_KEYS = frozenset(
    {"type", "properties", "items", "required", "additionalProperties", "__example__"}
)
_SCREEN_ID_PATTERN = re.compile(r"^[A-Za-z_]+$")
_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE_PATTERN = re.compile(r"\$\{([^{}]+)\}")
_LOCAL_REFERENCE_PATTERN = re.compile(
    r"^(?P<scope>form|data)\.(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_GLOBAL_REFERENCE_PATTERN = re.compile(
    r"^screen\.(?P<screen>[A-Za-z_]+)\.(?P<scope>form|data)\."
    r"(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)
_ACTION_PROPERTY_PATTERN = re.compile(r"^on-[a-z-]+-action$")


@dataclass(frozen=True, order=True)
class FlowJsonIssue:
    """One deterministic validation failure."""

    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class FlowJsonValidationReport:
    """Structured result returned by :func:`validate_flow_document`."""

    errors: tuple[FlowJsonIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [error.as_dict() for error in self.errors],
        }

    def raise_for_errors(self) -> None:
        if self.errors:
            raise FlowJsonValidationError(self.errors)


class FlowJsonValidationError(ValueError):
    """Raised when compilation would produce a non-publishable artifact."""

    def __init__(self, errors: Sequence[FlowJsonIssue]) -> None:
        self.errors = tuple(errors)
        preview = "; ".join(
            f"{issue.code} at {issue.path}" for issue in self.errors[:3]
        )
        remainder = len(self.errors) - 3
        if remainder > 0:
            preview = f"{preview}; and {remainder} more"
        super().__init__(preview or "invalid Flow JSON")

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": False,
            "errors": [error.as_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class FlowBlueprint:
    """Conceptual source model. It is not a Meta-uploadable JSON document."""

    name: str
    screens: tuple[Mapping[str, Any], ...]
    endpoint_driven: bool = False
    routing_model: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowJsonArtifact:
    """Canonical, validated bytes suitable for a Flow JSON asset upload."""

    blueprint_name: str
    canonical_json: str
    content_sha256: str
    byte_size: int

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)

    def as_bytes(self) -> bytes:
        return self.canonical_json.encode("utf-8")


@dataclass
class _ActionInfo:
    path: str
    property_name: str
    parent_component: str
    screen_id: str
    screen_terminal: bool
    action: Mapping[str, Any]


@dataclass
class _ScreenInfo:
    index: int
    screen_id: str
    terminal: bool
    data_schema: Mapping[str, Any]
    form_names: set[str] = field(default_factory=set)
    footer_paths: list[str] = field(default_factory=list)
    actions: list[_ActionInfo] = field(default_factory=list)


class _Validator:
    def __init__(self, document: Any) -> None:
        self.document = document
        self.errors: list[FlowJsonIssue] = []
        self.screen_infos: list[_ScreenInfo] = []
        self.screens_by_id: dict[str, _ScreenInfo] = {}
        self.has_data_exchange = False

    def run(self) -> FlowJsonValidationReport:
        self._validate_json_value(self.document, "$")
        self._validate_size()
        if not isinstance(self.document, Mapping):
            self.add("INVALID_DOCUMENT_TYPE", "$", "Flow JSON must be an object.")
            return self.report()

        self._validate_top_level()
        self._validate_screens()
        self._validate_references()
        self._validate_actions()
        self._validate_endpoint_contract()
        self._validate_routing()
        return self.report()

    def report(self) -> FlowJsonValidationReport:
        return FlowJsonValidationReport(tuple(sorted(set(self.errors))))

    def add(self, code: str, path: str, message: str) -> None:
        self.errors.append(FlowJsonIssue(path=path, code=code, message=message))

    def _validate_json_value(self, value: Any, path: str) -> None:
        if value is None:
            self.add("NULL_NOT_ALLOWED", path, "Flow JSON does not support null values.")
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    self.add(
                        "NON_STRING_OBJECT_KEY",
                        path,
                        "JSON object keys must be strings.",
                    )
                    continue
                self._validate_json_value(child, _child_path(path, key))
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                self._validate_json_value(child, f"{path}[{index}]")
            return
        if isinstance(value, bool) or isinstance(value, (str, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                self.add(
                    "NON_FINITE_NUMBER",
                    path,
                    "JSON numbers must be finite.",
                )
            return
        self.add(
            "NON_JSON_VALUE",
            path,
            f"Unsupported JSON value type: {type(value).__name__}.",
        )

    def _validate_size(self) -> None:
        try:
            byte_size = len(_serialize(self.document).encode("utf-8"))
        except (TypeError, ValueError):
            return
        if byte_size > MAX_FLOW_JSON_BYTES:
            self.add(
                "FLOW_JSON_TOO_LARGE",
                "$",
                f"Serialized Flow JSON is {byte_size} bytes; the limit is {MAX_FLOW_JSON_BYTES}.",
            )

    def _validate_top_level(self) -> None:
        for key in sorted(set(self.document) - _TOP_LEVEL_KEYS):
            self.add(
                "UNKNOWN_TOP_LEVEL_PROPERTY",
                _child_path("$", key),
                f"Property {key!r} is not valid in the supported Flow JSON 7.3 profile.",
            )

        if "version" not in self.document:
            self.add("MISSING_VERSION", "$.version", "Flow JSON version is required.")
        elif self.document.get("version") != FLOW_JSON_VERSION:
            self.add(
                "INVALID_VERSION",
                "$.version",
                f"Flow JSON version must be {FLOW_JSON_VERSION!r}.",
            )

        if "screens" not in self.document:
            self.add("MISSING_SCREENS", "$.screens", "At least one screen is required.")

        if "data_api_version" in self.document:
            if self.document.get("data_api_version") != DATA_API_VERSION:
                self.add(
                    "INVALID_DATA_API_VERSION",
                    "$.data_api_version",
                    f"Endpoint-driven Flow JSON must use data_api_version {DATA_API_VERSION!r}.",
                )

    def _validate_screens(self) -> None:
        screens = self.document.get("screens")
        if not isinstance(screens, list):
            if "screens" in self.document:
                self.add("INVALID_SCREENS_TYPE", "$.screens", "screens must be an array.")
            return
        if not screens:
            self.add("EMPTY_SCREENS", "$.screens", "At least one screen is required.")
            return
        if len(screens) > MAX_SCREENS:
            self.add(
                "TOO_MANY_SCREENS",
                "$.screens",
                f"A Flow can contain at most {MAX_SCREENS} screens.",
            )

        seen_ids: dict[str, int] = {}
        terminal_count = 0
        successful_terminal_count = 0

        for index, screen in enumerate(screens):
            path = f"$.screens[{index}]"
            if not isinstance(screen, Mapping):
                self.add("INVALID_SCREEN_TYPE", path, "Each screen must be an object.")
                continue

            for key in sorted(set(screen) - _SCREEN_KEYS):
                self.add(
                    "UNKNOWN_SCREEN_PROPERTY",
                    _child_path(path, key),
                    f"Property {key!r} is not valid on a Flow JSON 7.3 screen.",
                )

            screen_id = screen.get("id")
            valid_screen_id = isinstance(screen_id, str) and bool(
                _SCREEN_ID_PATTERN.fullmatch(screen_id)
            )
            if not isinstance(screen_id, str) or not screen_id:
                self.add("MISSING_SCREEN_ID", f"{path}.id", "Screen id is required.")
                normalized_id = f"__INVALID_SCREEN_{index}"
            else:
                normalized_id = screen_id
                if not valid_screen_id:
                    self.add(
                        "INVALID_SCREEN_ID",
                        f"{path}.id",
                        "Screen ids may contain only ASCII letters and underscores.",
                    )
                if screen_id.casefold() == "success":
                    self.add(
                        "RESERVED_SCREEN_ID",
                        f"{path}.id",
                        "SUCCESS is reserved by Meta and cannot be a screen id.",
                    )
                if screen_id in seen_ids:
                    self.add(
                        "DUPLICATE_SCREEN_ID",
                        f"{path}.id",
                        f"Screen id {screen_id!r} duplicates $.screens[{seen_ids[screen_id]}].id.",
                    )
                else:
                    seen_ids[screen_id] = index

            terminal = screen.get("terminal", False)
            if not isinstance(terminal, bool):
                self.add(
                    "INVALID_TERMINAL_TYPE",
                    f"{path}.terminal",
                    "terminal must be a boolean.",
                )
                terminal = False
            if terminal:
                terminal_count += 1

            success = screen.get("success")
            if success is not None and not isinstance(success, bool):
                self.add(
                    "INVALID_SUCCESS_TYPE",
                    f"{path}.success",
                    "success must be a boolean.",
                )
            if "success" in screen and not terminal:
                self.add(
                    "SUCCESS_REQUIRES_TERMINAL",
                    f"{path}.success",
                    "success can only be set on a terminal screen.",
                )
            if terminal and success is not False:
                successful_terminal_count += 1

            title = screen.get("title")
            if "title" in screen and (not isinstance(title, str) or not title.strip()):
                self.add(
                    "INVALID_SCREEN_TITLE",
                    f"{path}.title",
                    "title must be a non-empty string when provided.",
                )

            refresh_on_back = screen.get("refresh_on_back", False)
            if not isinstance(refresh_on_back, bool):
                self.add(
                    "INVALID_REFRESH_ON_BACK_TYPE",
                    f"{path}.refresh_on_back",
                    "refresh_on_back must be a boolean.",
                )

            data_schema = screen.get("data", {})
            if not isinstance(data_schema, Mapping):
                self.add(
                    "INVALID_SCREEN_DATA",
                    f"{path}.data",
                    "screen data must be an object.",
                )
                data_schema = {}
            else:
                self._validate_data_schema(data_schema, f"{path}.data")

            info = _ScreenInfo(
                index=index,
                screen_id=normalized_id,
                terminal=terminal,
                data_schema=data_schema,
            )
            self._validate_layout(screen.get("layout"), f"{path}.layout", info)
            self._collect_actions(screen, path, info)
            self._validate_sensitive(screen.get("sensitive"), f"{path}.sensitive", info)
            self._validate_terminal_footer(info)
            self.screen_infos.append(info)
            if valid_screen_id and screen_id not in self.screens_by_id:
                self.screens_by_id[screen_id] = info

        if terminal_count == 0:
            self.add(
                "MISSING_TERMINAL_SCREEN",
                "$.screens",
                "At least one terminal screen is required.",
            )
        elif successful_terminal_count == 0:
            self.add(
                "MISSING_SUCCESSFUL_TERMINAL",
                "$.screens",
                "At least one terminal screen must have success=true or use the default success value.",
            )

    def _validate_layout(
        self, layout: Any, path: str, info: _ScreenInfo
    ) -> None:
        if not isinstance(layout, Mapping):
            self.add("MISSING_LAYOUT", path, "Every screen requires a layout object.")
            return
        unknown = sorted(set(layout) - {"type", "children"})
        for key in unknown:
            self.add(
                "UNKNOWN_LAYOUT_PROPERTY",
                _child_path(path, key),
                f"Property {key!r} is not valid on SingleColumnLayout.",
            )
        if layout.get("type") != "SingleColumnLayout":
            self.add(
                "INVALID_LAYOUT_TYPE",
                f"{path}.type",
                "Flow JSON 7.3 supports SingleColumnLayout only.",
            )
        children = layout.get("children")
        if not isinstance(children, list):
            self.add(
                "INVALID_LAYOUT_CHILDREN",
                f"{path}.children",
                "layout children must be an array.",
            )
            return
        if not children:
            self.add(
                "EMPTY_LAYOUT",
                f"{path}.children",
                "A screen layout must contain at least one component.",
            )
        self._validate_component_list(children, f"{path}.children", info)

    def _validate_component_list(
        self, components: Any, path: str, info: _ScreenInfo
    ) -> None:
        if not isinstance(components, list):
            self.add("INVALID_COMPONENT_LIST", path, "Component children must be an array.")
            return
        for index, component in enumerate(components):
            self._validate_component(component, f"{path}[{index}]", info)

    def _validate_component(
        self, component: Any, path: str, info: _ScreenInfo
    ) -> None:
        if not isinstance(component, Mapping):
            self.add("INVALID_COMPONENT", path, "Each component must be an object.")
            return
        component_type = component.get("type")
        if not isinstance(component_type, str):
            self.add("MISSING_COMPONENT_TYPE", f"{path}.type", "Component type is required.")
            return
        if component_type not in SUPPORTED_COMPONENTS:
            self.add(
                "UNSUPPORTED_COMPONENT",
                f"{path}.type",
                f"Component type {component_type!r} is not in the supported Flow JSON 7.3 profile.",
            )
            return

        for property_name in _COMPONENT_REQUIRED_PROPERTIES.get(component_type, ()):
            if property_name not in component:
                self.add(
                    "MISSING_COMPONENT_PROPERTY",
                    _child_path(path, property_name),
                    f"{component_type} requires property {property_name!r}.",
                )

        if component_type == "Footer":
            info.footer_paths.append(path)
        if component_type in _FORM_VALUE_COMPONENTS:
            self._register_form_name(component.get("name"), f"{path}.name", info)

        if component_type == "Form":
            name = component.get("name")
            if not isinstance(name, str) or not _FIELD_NAME_PATTERN.fullmatch(name):
                self.add(
                    "INVALID_FORM_NAME",
                    f"{path}.name",
                    "Form names must start with a letter or underscore and contain only letters, digits, and underscores.",
                )
            self._validate_component_list(component.get("children"), f"{path}.children", info)
        elif component_type == "If":
            self._validate_component_list(component.get("then"), f"{path}.then", info)
            if "else" in component:
                self._validate_component_list(component.get("else"), f"{path}.else", info)
        elif component_type == "Switch":
            cases = component.get("cases")
            if not isinstance(cases, Mapping) or not cases:
                self.add(
                    "INVALID_SWITCH_CASES",
                    f"{path}.cases",
                    "Switch cases must be a non-empty object of component arrays.",
                )
            else:
                for case_name in sorted(cases):
                    self._validate_component_list(
                        cases[case_name], _child_path(f"{path}.cases", case_name), info
                    )

    def _register_form_name(self, name: Any, path: str, info: _ScreenInfo) -> None:
        if not isinstance(name, str) or not _FIELD_NAME_PATTERN.fullmatch(name):
            self.add(
                "INVALID_FORM_FIELD_NAME",
                path,
                "Form field names must start with a letter or underscore and contain only letters, digits, and underscores.",
            )
            return
        if name in info.form_names:
            self.add(
                "DUPLICATE_FORM_FIELD_NAME",
                path,
                f"Form field name {name!r} is duplicated on this screen.",
            )
            return
        info.form_names.add(name)

    def _validate_sensitive(self, sensitive: Any, path: str, info: _ScreenInfo) -> None:
        if sensitive is None:
            return
        if not isinstance(sensitive, list):
            self.add("INVALID_SENSITIVE_FIELDS", path, "sensitive must be an array of field names.")
            return
        seen: set[str] = set()
        for index, field_name in enumerate(sensitive):
            item_path = f"{path}[{index}]"
            if not isinstance(field_name, str):
                self.add("INVALID_SENSITIVE_FIELD", item_path, "Sensitive field names must be strings.")
            elif field_name not in info.form_names:
                self.add(
                    "UNKNOWN_SENSITIVE_FIELD",
                    item_path,
                    f"Sensitive field {field_name!r} is not a form field on this screen.",
                )
            elif field_name in seen:
                self.add(
                    "DUPLICATE_SENSITIVE_FIELD",
                    item_path,
                    f"Sensitive field {field_name!r} is listed more than once.",
                )
            else:
                seen.add(field_name)

    def _validate_terminal_footer(self, info: _ScreenInfo) -> None:
        if len(info.footer_paths) > 1:
            for path in info.footer_paths[1:]:
                self.add(
                    "MORE_THAN_ONE_FOOTER",
                    path,
                    "A screen can contain at most one Footer across all component branches.",
                )
        if info.terminal and not info.footer_paths:
            self.add(
                "MISSING_TERMINAL_FOOTER",
                f"$.screens[{info.index}].layout",
                "A terminal screen requires a Footer.",
            )

    def _validate_data_schema(self, data: Mapping[str, Any], path: str) -> None:
        for field_name in sorted(data):
            field_path = _child_path(path, field_name)
            if not _FIELD_NAME_PATTERN.fullmatch(field_name):
                self.add(
                    "INVALID_DATA_FIELD_NAME",
                    field_path,
                    "Data field names must start with a letter or underscore and contain only letters, digits, and underscores.",
                )
            self._validate_schema_node(data[field_name], field_path, top_level=True)

    def _validate_schema_node(self, schema: Any, path: str, *, top_level: bool) -> None:
        if not isinstance(schema, Mapping):
            self.add("INVALID_DATA_SCHEMA", path, "Each data field must contain a schema object.")
            return
        for key in sorted(set(schema) - _SCHEMA_KEYS):
            self.add(
                "UNKNOWN_DATA_SCHEMA_PROPERTY",
                _child_path(path, key),
                f"Unsupported data schema property {key!r}.",
            )
        schema_type = schema.get("type")
        if schema_type not in _SCHEMA_TYPES:
            self.add(
                "INVALID_DATA_SCHEMA_TYPE",
                f"{path}.type",
                f"Data schema type must be one of {sorted(_SCHEMA_TYPES)}.",
            )
            return
        if top_level and "__example__" not in schema:
            self.add(
                "MISSING_DATA_EXAMPLE",
                f"{path}.__example__",
                "Every top-level screen data field requires __example__.",
            )
        if not top_level and "__example__" in schema:
            self.add(
                "NESTED_DATA_EXAMPLE",
                f"{path}.__example__",
                "__example__ is allowed only on top-level screen data fields.",
            )

        if schema_type == "object":
            properties = schema.get("properties")
            if not isinstance(properties, Mapping):
                self.add(
                    "MISSING_OBJECT_PROPERTIES",
                    f"{path}.properties",
                    "Object data schemas require a properties object.",
                )
            else:
                for field_name in sorted(properties):
                    if not _FIELD_NAME_PATTERN.fullmatch(field_name):
                        self.add(
                            "INVALID_DATA_FIELD_NAME",
                            _child_path(f"{path}.properties", field_name),
                            "Nested data field names must use letters, digits, and underscores.",
                        )
                    self._validate_schema_node(
                        properties[field_name],
                        _child_path(f"{path}.properties", field_name),
                        top_level=False,
                    )
        elif schema_type == "array":
            items = schema.get("items")
            if not isinstance(items, (Mapping, bool)):
                self.add(
                    "MISSING_ARRAY_ITEMS",
                    f"{path}.items",
                    "Array data schemas require items as a schema object or boolean.",
                )
            elif isinstance(items, Mapping):
                self._validate_schema_node(items, f"{path}.items", top_level=False)

        if "__example__" in schema and not _example_matches_type(
            schema["__example__"], schema_type
        ):
            self.add(
                "DATA_EXAMPLE_TYPE_MISMATCH",
                f"{path}.__example__",
                f"__example__ must match data schema type {schema_type!r}.",
            )

    def _collect_actions(
        self,
        value: Any,
        path: str,
        info: _ScreenInfo,
        parent_component: str = "",
    ) -> None:
        if isinstance(value, Mapping):
            current_component = parent_component
            component_type = value.get("type")
            if isinstance(component_type, str) and component_type in SUPPORTED_COMPONENTS:
                current_component = component_type
            for key, child in value.items():
                child_path = _child_path(path, key)
                if key in ACTION_PROPERTIES:
                    action = child if isinstance(child, Mapping) else {}
                    if not isinstance(child, Mapping):
                        self.add(
                            "INVALID_ACTION_TYPE",
                            child_path,
                            "Action properties must contain an object.",
                        )
                    info.actions.append(
                        _ActionInfo(
                            path=child_path,
                            property_name=key,
                            parent_component=current_component,
                            screen_id=info.screen_id,
                            screen_terminal=info.terminal,
                            action=action,
                        )
                    )
                elif _ACTION_PROPERTY_PATTERN.fullmatch(key):
                    self.add(
                        "UNSUPPORTED_ACTION_PROPERTY",
                        child_path,
                        f"Action property {key!r} is not supported in Flow JSON 7.3.",
                    )
                else:
                    self._collect_actions(child, child_path, info, current_component)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._collect_actions(
                    child, f"{path}[{index}]", info, parent_component
                )

    def _validate_references(self) -> None:
        screens = self.document.get("screens")
        if not isinstance(screens, list):
            return
        for info in self.screen_infos:
            if info.index >= len(screens):
                continue
            self._validate_reference_value(
                screens[info.index], f"$.screens[{info.index}]", info
            )

    def _validate_reference_value(
        self, value: Any, path: str, current: _ScreenInfo
    ) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                self._validate_reference_value(child, _child_path(path, key), current)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                self._validate_reference_value(child, f"{path}[{index}]", current)
            return
        if not isinstance(value, str) or "${" not in value:
            return

        matches = list(_REFERENCE_PATTERN.finditer(value))
        remainder = _REFERENCE_PATTERN.sub("", value)
        if not matches or "${" in remainder:
            self.add(
                "MALFORMED_DYNAMIC_REFERENCE",
                path,
                "Dynamic references must use ${form.field}, ${data.field}, or ${screen.SCREEN.scope.field}.",
            )
        for match in matches:
            expression = match.group(1)
            local = _LOCAL_REFERENCE_PATTERN.fullmatch(expression)
            if local:
                self._validate_reference_target(
                    current,
                    local.group("scope"),
                    local.group("field"),
                    path,
                    expression,
                )
                continue
            global_reference = _GLOBAL_REFERENCE_PATTERN.fullmatch(expression)
            if global_reference:
                target_id = global_reference.group("screen")
                target = self.screens_by_id.get(target_id)
                if target is None:
                    self.add(
                        "UNKNOWN_REFERENCE_SCREEN",
                        path,
                        f"Dynamic reference targets unknown screen {target_id!r}.",
                    )
                elif target_id == current.screen_id:
                    self.add(
                        "GLOBAL_REFERENCE_TO_CURRENT_SCREEN",
                        path,
                        "Use a local form/data reference for the current screen.",
                    )
                else:
                    self._validate_reference_target(
                        target,
                        global_reference.group("scope"),
                        global_reference.group("field"),
                        path,
                        expression,
                    )
                continue
            self.add(
                "INVALID_DYNAMIC_REFERENCE",
                path,
                f"Unsupported dynamic reference ${{{expression}}}.",
            )

    def _validate_reference_target(
        self,
        target: _ScreenInfo,
        scope: str,
        field_path: str,
        path: str,
        expression: str,
    ) -> None:
        if scope == "form":
            if "." in field_path or field_path not in target.form_names:
                self.add(
                    "UNKNOWN_FORM_REFERENCE",
                    path,
                    f"Dynamic reference ${{{expression}}} does not match a form field.",
                )
            return
        if not _schema_has_path(target.data_schema, field_path.split(".")):
            self.add(
                "UNKNOWN_DATA_REFERENCE",
                path,
                f"Dynamic reference ${{{expression}}} is not declared in screen data.",
            )

    def _validate_actions(self) -> None:
        for info in self.screen_infos:
            for action_info in info.actions:
                action = action_info.action
                name = action.get("name")
                if name not in ALLOWED_ACTIONS:
                    self.add(
                        "INVALID_ACTION_NAME",
                        f"{action_info.path}.name",
                        f"Action name must be one of {sorted(ALLOWED_ACTIONS)}.",
                    )
                    continue
                if name == "data_exchange":
                    self.has_data_exchange = True

                allowed_keys = _ACTION_KEYS[name]
                for key in sorted(set(action) - allowed_keys):
                    self.add(
                        "UNKNOWN_ACTION_PROPERTY",
                        _child_path(action_info.path, key),
                        f"Property {key!r} is not valid for action {name!r}.",
                    )

                if name in {"navigate", "data_exchange", "complete", "update_data"}:
                    payload = action.get("payload")
                    if not isinstance(payload, Mapping):
                        self.add(
                            "INVALID_ACTION_PAYLOAD",
                            f"{action_info.path}.payload",
                            f"Action {name!r} requires an object payload (empty is allowed except for update_data).",
                        )
                    elif name == "update_data":
                        if not payload:
                            self.add(
                                "EMPTY_UPDATE_DATA_PAYLOAD",
                                f"{action_info.path}.payload",
                                "update_data requires at least one screen data field.",
                            )
                        for key in payload:
                            if key not in info.data_schema:
                                self.add(
                                    "UNKNOWN_UPDATE_DATA_FIELD",
                                    _child_path(f"{action_info.path}.payload", key),
                                    f"update_data field {key!r} is not declared in this screen's data model.",
                                )
                    elif name == "complete" and isinstance(payload, Mapping):
                        self._validate_complete_payload(payload, f"{action_info.path}.payload")

                if name == "navigate":
                    self._validate_navigate_action(action, action_info)
                elif name == "complete":
                    if not action_info.screen_terminal:
                        self.add(
                            "COMPLETE_REQUIRES_TERMINAL",
                            action_info.path,
                            "complete can only be used on a terminal screen.",
                        )
                    if action_info.parent_component != "Footer":
                        self.add(
                            "COMPLETE_REQUIRES_FOOTER",
                            action_info.path,
                            "complete must be the on-click-action of a Footer.",
                        )
                elif name == "open_url":
                    self._validate_open_url_action(action, action_info)

                if action_info.parent_component == "Footer" and name not in {
                    "navigate",
                    "data_exchange",
                    "complete",
                }:
                    self.add(
                        "INVALID_FOOTER_ACTION",
                        action_info.path,
                        "Footer actions must be navigate, data_exchange, or complete.",
                    )

            if info.terminal:
                footer_actions = [
                    action
                    for action in info.actions
                    if action.parent_component == "Footer"
                    and action.property_name == "on-click-action"
                ]
                if info.footer_paths and not any(
                    action.action.get("name") == "complete" for action in footer_actions
                ):
                    self.add(
                        "TERMINAL_FOOTER_MUST_COMPLETE",
                        info.footer_paths[0],
                        "The Footer on a terminal screen must use complete.",
                    )

    def _validate_navigate_action(
        self, action: Mapping[str, Any], info: _ActionInfo
    ) -> None:
        if info.screen_terminal:
            self.add(
                "NAVIGATE_ON_TERMINAL",
                info.path,
                "navigate cannot be used on a terminal screen.",
            )
        next_screen = action.get("next")
        if not isinstance(next_screen, Mapping):
            self.add(
                "INVALID_NAVIGATE_TARGET",
                f"{info.path}.next",
                "navigate requires next={type: 'screen', name: 'SCREEN_ID'}.",
            )
            return
        if set(next_screen) != {"type", "name"} or next_screen.get("type") != "screen":
            self.add(
                "INVALID_NAVIGATE_TARGET",
                f"{info.path}.next",
                "navigate next must contain exactly type='screen' and a screen name.",
            )
        target = next_screen.get("name")
        if not isinstance(target, str) or target not in self.screens_by_id:
            self.add(
                "UNKNOWN_NAVIGATE_TARGET",
                f"{info.path}.next.name",
                f"navigate targets unknown screen {target!r}.",
            )

    def _validate_open_url_action(
        self, action: Mapping[str, Any], info: _ActionInfo
    ) -> None:
        if info.parent_component not in {"EmbeddedLink", "OptIn"}:
            self.add(
                "INVALID_OPEN_URL_COMPONENT",
                info.path,
                "open_url is supported only by EmbeddedLink and OptIn.",
            )
        url = action.get("url")
        if not isinstance(url, str) or not url:
            self.add("INVALID_OPEN_URL", f"{info.path}.url", "open_url requires a URL string.")
            return
        if "${" in url:
            return
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            self.add(
                "INVALID_OPEN_URL",
                f"{info.path}.url",
                "Static open_url targets must be credential-free HTTPS URLs.",
            )

    def _validate_complete_payload(self, payload: Mapping[str, Any], path: str) -> None:
        for key, value in payload.items():
            self._validate_complete_payload_value(value, _child_path(path, key))

    def _validate_complete_payload_value(self, value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                self._validate_complete_payload_value(child, _child_path(path, key))
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                self._validate_complete_payload_value(child, f"{path}[{index}]")
            return
        if not isinstance(value, str):
            self.add(
                "COMPLETE_PAYLOAD_NOT_USER_DATA",
                path,
                "complete payload leaves must be direct references to user-entered form fields.",
            )
            return
        local = _LOCAL_REFERENCE_PATTERN.fullmatch(
            value[2:-1] if value.startswith("${") and value.endswith("}") else ""
        )
        global_reference = _GLOBAL_REFERENCE_PATTERN.fullmatch(
            value[2:-1] if value.startswith("${") and value.endswith("}") else ""
        )
        if not (
            (local and local.group("scope") == "form")
            or (global_reference and global_reference.group("scope") == "form")
        ):
            self.add(
                "COMPLETE_PAYLOAD_NOT_USER_DATA",
                path,
                "complete payload leaves must be direct references to user-entered form fields.",
            )

    def _validate_endpoint_contract(self) -> None:
        data_api_present = "data_api_version" in self.document
        routing_present = "routing_model" in self.document
        endpoint_driven = data_api_present or self.has_data_exchange

        if self.has_data_exchange and not data_api_present:
            self.add(
                "DATA_API_VERSION_REQUIRED",
                "$.data_api_version",
                "data_exchange requires data_api_version='3.0'.",
            )
        if endpoint_driven and not routing_present:
            self.add(
                "ROUTING_MODEL_REQUIRED",
                "$.routing_model",
                "Endpoint-driven Flow JSON requires routing_model.",
            )

        for info in self.screen_infos:
            screens = self.document.get("screens", [])
            if info.index >= len(screens) or not isinstance(screens[info.index], Mapping):
                continue
            if screens[info.index].get("refresh_on_back") is True and not data_api_present:
                self.add(
                    "REFRESH_ON_BACK_REQUIRES_ENDPOINT",
                    f"$.screens[{info.index}].refresh_on_back",
                    "refresh_on_back=true requires data_api_version='3.0'.",
                )

    def _validate_routing(self) -> None:
        routing = self.document.get("routing_model")
        endpoint_driven = "data_api_version" in self.document or self.has_data_exchange
        navigate_edges = self._navigate_edges()

        if routing is None:
            if not endpoint_driven:
                self._validate_graph(navigate_edges)
            return
        if not isinstance(routing, Mapping):
            self.add(
                "INVALID_ROUTING_MODEL",
                "$.routing_model",
                "routing_model must be an object mapping screen ids to arrays.",
            )
            return

        screen_ids = set(self.screens_by_id)
        routing_keys = set(routing)
        if endpoint_driven:
            for missing in sorted(screen_ids - routing_keys):
                self.add(
                    "MISSING_ROUTING_SCREEN",
                    "$.routing_model",
                    f"Endpoint routing_model is missing screen {missing!r}.",
                )
            for unknown in sorted(routing_keys - screen_ids):
                self.add(
                    "UNKNOWN_ROUTING_SCREEN",
                    _child_path("$.routing_model", unknown),
                    f"routing_model contains unknown source screen {unknown!r}.",
                )

        edges: dict[str, set[str]] = {screen_id: set() for screen_id in screen_ids}
        for source in sorted(routing, key=str):
            source_path = _child_path("$.routing_model", source) if isinstance(source, str) else "$.routing_model"
            targets = routing[source]
            if not isinstance(source, str):
                continue
            if source not in screen_ids:
                if not endpoint_driven:
                    self.add(
                        "UNKNOWN_ROUTING_SCREEN",
                        source_path,
                        f"routing_model contains unknown source screen {source!r}.",
                    )
                continue
            if not isinstance(targets, list):
                self.add(
                    "INVALID_ROUTING_TARGETS",
                    source_path,
                    "Each routing_model value must be an array of screen ids.",
                )
                continue
            if len(targets) > MAX_ROUTING_BRANCHES:
                self.add(
                    "TOO_MANY_ROUTING_BRANCHES",
                    source_path,
                    f"A screen can have at most {MAX_ROUTING_BRANCHES} forward routes.",
                )
            seen_targets: set[str] = set()
            for index, target in enumerate(targets):
                target_path = f"{source_path}[{index}]"
                if not isinstance(target, str):
                    self.add(
                        "INVALID_ROUTING_TARGET",
                        target_path,
                        "Route targets must be screen id strings.",
                    )
                    continue
                if target in seen_targets:
                    self.add(
                        "DUPLICATE_ROUTING_TARGET",
                        target_path,
                        f"Route to {target!r} is duplicated.",
                    )
                    continue
                seen_targets.add(target)
                if target == source:
                    self.add(
                        "SELF_ROUTING_NOT_ALLOWED",
                        target_path,
                        "A forward route cannot target its source screen.",
                    )
                elif target not in screen_ids:
                    self.add(
                        "UNKNOWN_ROUTING_TARGET",
                        target_path,
                        f"Route targets unknown screen {target!r}.",
                    )
                else:
                    edges[source].add(target)

        for source, targets in sorted(edges.items()):
            for target in sorted(targets):
                if source in edges.get(target, set()):
                    self.add(
                        "REVERSE_ROUTE_NOT_ALLOWED",
                        _child_path("$.routing_model", target),
                        f"Only the forward route {source!r} -> {target!r} may be declared.",
                    )

        if endpoint_driven:
            for source, target, action_path in navigate_edges:
                if target not in edges.get(source, set()):
                    self.add(
                        "NAVIGATE_ROUTE_MISSING",
                        action_path,
                        f"navigate route {source!r} -> {target!r} is missing from routing_model.",
                    )
        else:
            for source, target, _ in navigate_edges:
                edges.setdefault(source, set()).add(target)

        self._validate_graph(
            [(source, target, "$.routing_model") for source, targets in edges.items() for target in targets],
            explicit_edges=edges,
        )

    def _navigate_edges(self) -> list[tuple[str, str, str]]:
        edges: list[tuple[str, str, str]] = []
        for info in self.screen_infos:
            if info.screen_id not in self.screens_by_id:
                continue
            for action_info in info.actions:
                if action_info.action.get("name") != "navigate":
                    continue
                next_screen = action_info.action.get("next")
                if isinstance(next_screen, Mapping) and isinstance(next_screen.get("name"), str):
                    edges.append(
                        (info.screen_id, next_screen["name"], action_info.path)
                    )
        return edges

    def _validate_graph(
        self,
        edge_rows: Sequence[tuple[str, str, str]],
        *,
        explicit_edges: Mapping[str, set[str]] | None = None,
    ) -> None:
        screen_ids = set(self.screens_by_id)
        if not screen_ids:
            return
        edges: dict[str, set[str]] = {screen_id: set() for screen_id in screen_ids}
        if explicit_edges is not None:
            for source, targets in explicit_edges.items():
                if source in edges:
                    edges[source].update(target for target in targets if target in screen_ids)
        else:
            for source, target, _ in edge_rows:
                if source in screen_ids and target in screen_ids:
                    edges[source].add(target)

        indegree = {screen_id: 0 for screen_id in screen_ids}
        undirected = {screen_id: set() for screen_id in screen_ids}
        for source, targets in edges.items():
            for target in targets:
                indegree[target] += 1
                undirected[source].add(target)
                undirected[target].add(source)

        roots = sorted(screen_id for screen_id, count in indegree.items() if count == 0)
        if not roots:
            self.add(
                "MISSING_ENTRY_SCREEN",
                "$.routing_model" if "routing_model" in self.document else "$.screens",
                "The Flow graph requires at least one entry screen with no incoming route.",
            )

        start = sorted(screen_ids)[0]
        visited: set[str] = set()
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            queue.extend(sorted(undirected[current] - visited))
        for disconnected in sorted(screen_ids - visited):
            self.add(
                "DISCONNECTED_SCREEN",
                f"$.screens[{self.screens_by_id[disconnected].index}].id",
                f"Screen {disconnected!r} is disconnected from the Flow graph.",
            )

        cycle_nodes = _find_cycle(edges)
        if cycle_nodes:
            self.add(
                "ROUTING_CYCLE",
                "$.routing_model" if "routing_model" in self.document else "$.screens",
                f"Forward routes contain a cycle: {' -> '.join(cycle_nodes)}.",
            )

        for screen_id in sorted(screen_ids):
            info = self.screens_by_id[screen_id]
            targets = edges[screen_id]
            if info.terminal and targets:
                self.add(
                    "TERMINAL_SCREEN_HAS_ROUTE",
                    _child_path("$.routing_model", screen_id)
                    if "routing_model" in self.document
                    else f"$.screens[{info.index}]",
                    "Terminal screens cannot have forward routes.",
                )
            elif not info.terminal and not targets:
                self.add(
                    "ROUTE_DOES_NOT_REACH_TERMINAL",
                    f"$.screens[{info.index}]",
                    "Every non-terminal screen needs a forward route to a terminal screen.",
                )


def validate_flow_document(document: Any) -> FlowJsonValidationReport:
    """Return all deterministic structural errors for a Flow JSON document."""

    return _Validator(document).run()


def assert_valid_flow_document(document: Any) -> None:
    """Raise :class:`FlowJsonValidationError` if ``document`` is invalid."""

    validate_flow_document(document).raise_for_errors()


def canonical_flow_json(document: Any) -> str:
    """Return canonical JSON only after the document passes strict validation."""

    assert_valid_flow_document(document)
    return _serialize(document)


def flow_json_sha256(document: Any) -> str:
    """Return a deterministic content digest; this is not a signature."""

    canonical = canonical_flow_json(document)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_flow_blueprint(blueprint: FlowBlueprint) -> FlowJsonArtifact:
    """Compile a conceptual blueprint into a validated Flow JSON 7.3 artifact."""

    if not isinstance(blueprint, FlowBlueprint):
        raise TypeError("compile_flow_blueprint requires a FlowBlueprint")
    if not blueprint.name.strip():
        raise ValueError("blueprint name must be non-empty")

    document: dict[str, Any] = {
        "version": FLOW_JSON_VERSION,
        "screens": [deepcopy(dict(screen)) for screen in blueprint.screens],
    }
    if blueprint.endpoint_driven:
        document["data_api_version"] = DATA_API_VERSION
    if blueprint.endpoint_driven or blueprint.routing_model:
        document["routing_model"] = {
            str(source): list(targets)
            for source, targets in blueprint.routing_model.items()
        }

    report = validate_flow_document(document)
    report.raise_for_errors()
    canonical = _serialize(document)
    encoded = canonical.encode("utf-8")
    return FlowJsonArtifact(
        blueprint_name=blueprint.name,
        canonical_json=canonical,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        byte_size=len(encoded),
    )


def build_claim_tracking_blueprint() -> FlowBlueprint:
    """Build an endpoint-driven claim lookup plus optional follow-up note."""

    return FlowBlueprint(
        name="claim_tracking",
        endpoint_driven=True,
        routing_model={
            "CLAIM_LOOKUP": ("CLAIM_RESULT",),
            "CLAIM_RESULT": (),
        },
        screens=(
            {
                "id": "CLAIM_LOOKUP",
                "title": "Seguir reclamo",
                "sensitive": ["access_pin"],
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "TextHeading",
                            "text": "Consulta el estado de tu reclamo",
                        },
                        {
                            "type": "TextBody",
                            "text": "Ingresa los datos entregados al crear el reclamo.",
                        },
                        {
                            "type": "TextInput",
                            "name": "ticket_number",
                            "label": "Numero de reclamo",
                            "required": True,
                        },
                        {
                            "type": "TextInput",
                            "name": "access_pin",
                            "label": "PIN de acceso",
                            "required": True,
                        },
                        {
                            "type": "Footer",
                            "label": "Consultar",
                            "on-click-action": {
                                "name": "data_exchange",
                                "payload": {
                                    "ticket_number": "${form.ticket_number}",
                                    "access_pin": "${form.access_pin}",
                                },
                            },
                        },
                    ],
                },
            },
            {
                "id": "CLAIM_RESULT",
                "title": "Estado del reclamo",
                "terminal": True,
                "success": True,
                "data": {
                    "status": {"type": "string", "__example__": "En revision"},
                    "last_update": {
                        "type": "string",
                        "__example__": "Actualizado hoy",
                    },
                    "summary": {
                        "type": "string",
                        "__example__": "El equipo municipal esta revisando el caso.",
                    },
                },
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {"type": "TextHeading", "text": "${data.status}"},
                        {"type": "TextCaption", "text": "${data.last_update}"},
                        {"type": "TextBody", "text": "${data.summary}"},
                        {
                            "type": "TextArea",
                            "name": "follow_up_note",
                            "label": "Agregar comentario (opcional)",
                            "required": False,
                        },
                        {
                            "type": "Footer",
                            "label": "Finalizar",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {
                                    "ticket_number": "${screen.CLAIM_LOOKUP.form.ticket_number}",
                                    "follow_up_note": "${form.follow_up_note}",
                                },
                            },
                        },
                    ],
                },
            },
        ),
    )


def build_order_checkout_blueprint() -> FlowBlueprint:
    """Build an endpoint-driven order review with explicit user confirmation."""

    return FlowBlueprint(
        name="order_checkout",
        endpoint_driven=True,
        routing_model={
            "ORDER_DETAILS": ("ORDER_CONFIRM",),
            "ORDER_CONFIRM": (),
        },
        screens=(
            {
                "id": "ORDER_DETAILS",
                "title": "Datos del pedido",
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "TextHeading",
                            "text": "Completa la entrega",
                        },
                        {
                            "type": "TextInput",
                            "name": "full_name",
                            "label": "Nombre completo",
                            "required": True,
                        },
                        {
                            "type": "TextInput",
                            "name": "phone",
                            "label": "Telefono",
                            "required": True,
                            "input-type": "phone",
                        },
                        {
                            "type": "TextInput",
                            "name": "delivery_address",
                            "label": "Direccion de entrega",
                            "required": True,
                        },
                        {
                            "type": "TextArea",
                            "name": "delivery_notes",
                            "label": "Indicaciones (opcional)",
                            "required": False,
                        },
                        {
                            "type": "Footer",
                            "label": "Revisar pedido",
                            "on-click-action": {
                                "name": "data_exchange",
                                "payload": {
                                    "full_name": "${form.full_name}",
                                    "phone": "${form.phone}",
                                    "delivery_address": "${form.delivery_address}",
                                    "delivery_notes": "${form.delivery_notes}",
                                },
                            },
                        },
                    ],
                },
            },
            {
                "id": "ORDER_CONFIRM",
                "title": "Confirmar pedido",
                "terminal": True,
                "success": True,
                "data": {
                    "order_summary": {
                        "type": "string",
                        "__example__": "2 productos",
                    },
                    "total_display": {
                        "type": "string",
                        "__example__": "$ 25.000",
                    },
                },
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {"type": "TextHeading", "text": "${data.order_summary}"},
                        {
                            "type": "TextBody",
                            "text": "Total: ${data.total_display}",
                        },
                        {
                            "type": "OptIn",
                            "name": "confirm_order",
                            "label": "Confirmo los datos y el pedido",
                            "required": True,
                        },
                        {
                            "type": "Footer",
                            "label": "Confirmar",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {
                                    "full_name": "${screen.ORDER_DETAILS.form.full_name}",
                                    "phone": "${screen.ORDER_DETAILS.form.phone}",
                                    "delivery_address": "${screen.ORDER_DETAILS.form.delivery_address}",
                                    "delivery_notes": "${screen.ORDER_DETAILS.form.delivery_notes}",
                                    "confirm_order": "${form.confirm_order}",
                                },
                            },
                        },
                    ],
                },
            },
        ),
    )


def build_claim_tracking_flow() -> FlowJsonArtifact:
    return compile_flow_blueprint(build_claim_tracking_blueprint())


def build_order_checkout_flow() -> FlowJsonArtifact:
    return compile_flow_blueprint(build_order_checkout_blueprint())


def _serialize(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _child_path(path: str, key: str) -> str:
    if _FIELD_NAME_PATTERN.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=True)}]"


def _example_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, list)
    return False


def _schema_has_path(data_schema: Mapping[str, Any], segments: Sequence[str]) -> bool:
    if not segments or segments[0] not in data_schema:
        return False
    schema = data_schema[segments[0]]
    for segment in segments[1:]:
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            return False
        properties = schema.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            return False
        schema = properties[segment]
    return isinstance(schema, Mapping)


def _find_cycle(edges: Mapping[str, set[str]]) -> list[str]:
    state: dict[str, int] = defaultdict(int)
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        stack.append(node)
        for target in sorted(edges.get(node, set())):
            if state[target] == 0:
                cycle = visit(target)
                if cycle:
                    return cycle
            elif state[target] == 1:
                start = stack.index(target)
                return stack[start:] + [target]
        stack.pop()
        state[node] = 2
        return []

    for node in sorted(edges):
        if state[node] == 0:
            cycle = visit(node)
            if cycle:
                return cycle
    return []


__all__ = [
    "ALLOWED_ACTIONS",
    "DATA_API_VERSION",
    "FLOW_JSON_VERSION",
    "MAX_FLOW_JSON_BYTES",
    "FlowBlueprint",
    "FlowJsonArtifact",
    "FlowJsonIssue",
    "FlowJsonValidationError",
    "FlowJsonValidationReport",
    "assert_valid_flow_document",
    "build_claim_tracking_blueprint",
    "build_claim_tracking_flow",
    "build_order_checkout_blueprint",
    "build_order_checkout_flow",
    "canonical_flow_json",
    "compile_flow_blueprint",
    "flow_json_sha256",
    "validate_flow_document",
]
