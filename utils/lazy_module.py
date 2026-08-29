"""Thread-safe proxy for optional SDKs that are expensive to import."""

from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any


class LazyModule:
    """Load ``module_name`` on first attribute access, never at construction."""

    _INTERNAL_ATTRIBUTES = {"_module_name", "_module", "_import_error", "_lock"}

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: ModuleType | None = None
        self._import_error: Exception | None = None
        self._lock = threading.Lock()

    def _load(self) -> ModuleType:
        module = self._module
        if module is not None:
            return module
        if self._import_error is not None:
            raise self._import_error

        with self._lock:
            if self._module is not None:
                return self._module
            if self._import_error is not None:
                raise self._import_error
            try:
                module = importlib.import_module(self._module_name)
            except Exception as exc:
                self._import_error = exc
                raise
            self._module = module
            return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._INTERNAL_ATTRIBUTES:
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)

    def __delattr__(self, name: str) -> None:
        if name in self._INTERNAL_ATTRIBUTES:
            object.__delattr__(self, name)
            return
        delattr(self._load(), name)

    def __dir__(self) -> list[str]:
        try:
            names = dir(self._load())
        except Exception:
            names = []
        return sorted(set(object.__dir__(self)) | set(names))

    def __bool__(self) -> bool:
        try:
            self._load()
        except Exception:
            return False
        return True

    def __repr__(self) -> str:
        state = "loaded" if self._module is not None else "pending"
        if self._import_error is not None:
            state = "unavailable"
        return f"<LazyModule {self._module_name!r} state={state}>"
