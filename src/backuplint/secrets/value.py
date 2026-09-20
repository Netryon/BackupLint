"""Secret value wrapper that resists accidental disclosure."""

from __future__ import annotations

from typing import Any


class SecretValue:
    """In-memory secret bytes/text that redacts under str/repr/format/JSON.

    Call :meth:`get_secret_value` only at the consumption boundary (for example
    when populating a subprocess environment). Prefer discarding references
    promptly after use.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretValue requires a str")
        object.__setattr__(self, "_value", value)

    def get_secret_value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "***"

    def __repr__(self) -> str:
        return "SecretValue(***)"

    def __format__(self, format_spec: str) -> str:
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("SecretValue", self._value))

    def __reduce__(self) -> Any:
        # Refuse naive pickle of secret material.
        raise TypeError("SecretValue cannot be pickled")

    @property
    def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
        # Block dataclass/vars-style leakage of the real value.
        return {"redacted": True}
