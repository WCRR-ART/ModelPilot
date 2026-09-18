"""Strict data-only JSON parsing shared by definitions and the file loader."""

import json
from math import isfinite


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def _float(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def parse_json_document(text: str) -> object:
    return json.loads(text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
