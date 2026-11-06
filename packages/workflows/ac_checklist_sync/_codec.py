"""Codec for the "Checklist for Jira Cloud" (Appbox, add-on key
`com.appbox.ai.checklist`) entity-property serialization format.

This Jira Marketplace app stores each issue's checklist (all tabs/sections/
items) as one or more Jira issue entity properties, keyed
`com.appbox.ai.checklist-ticket-checklist` (metadata: total size, chunk
count, object guid, last-changed timestamp) plus `..._0`, `..._1`, ... shard
properties holding the actual payload, split into chunks only when the
payload exceeds the app's own size threshold.

The app does not publish a REST API for "multiple checklists" (multiple tabs
per issue) - its documented custom-field sync (`Checklist Text`/`Checklist
YAML`) is explicitly disabled whenever an issue uses more than one checklist/
tab, which this project's Jira instance always does. This module was built by
reverse-engineering the payload format directly (read access is always safe;
see the format notes below for what was verified before any write was ever
attempted against a real issue).

Payload shape: `json.loads(shard_property["data"])` yields `[flat, root_ref]`
where `flat` is a list of tokens and `root_ref` is a base62-encoded index into
`flat` pointing at the root object. Each entry in `flat` is either:
  - a literal string (dictionary value - a property name, GUID, or arbitrary
    text content), OR
  - a tagged token:
      "n|<base62>"  -> a number, encoded with the SAME base62 alphabet used
                       for reference indices (NOT hex - small values like
                       "n|5"/"n|1c" happen to look hex-plausible, but larger
                       ones like "n|H" (=17) or "n|v" (=57) only decode
                       correctly as base62; verified against live
                       `stats.totalItems`/`checklistProgress` on a real
                       issue, which must agree).
      "b|T" / "b|F" -> boolean true/false.
      "a|i1|i2|..." -> an array - each `iN` is a base62 ref to another flat
                       index. Used both for real array VALUES (e.g. an
                       item `list`) and, when referenced as the first ref of
                       an "o|" entry, as that object's ordered key-name list
                       ("shape").
      "o|s|v1|v2|..." -> an object - `s` is a base62 ref to the "a|" shape
                       entry (ordered key-name indices); each subsequent
                       `vN` is a base62 ref to that key's value, in the same
                       order as the shape's keys.

Reference sharing is structural: two items with an identical status (e.g.
the same "To Do" status object) point at the SAME flat index, and the
decoder/encoder here preserve that via Python object identity (`id(obj)`),
mirroring how `tabs`/`sections`/items literally share status sub-objects in
real checklist data.
"""

from __future__ import annotations

import json
from typing import Any

B62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def enc_b62(n: int) -> str:
    if n == 0:
        return "0"
    digits = ""
    while n > 0:
        n, remainder = divmod(n, 62)
        digits = B62_ALPHABET[remainder] + digits
    return digits


def dec_b62(token: str) -> int:
    value = 0
    for ch in token:
        value = value * 62 + B62_ALPHABET.index(ch)
    return value


def decode_flat(flat: list[Any], root_idx: int) -> Any:
    """Resolve the flat token array into a normal nested Python structure
    (dict/list/str/int/bool), starting from `root_idx`."""
    cache: dict[int, Any] = {}

    def resolve(idx: int) -> Any:
        if idx in cache:
            return cache[idx]
        raw = flat[idx]
        cache[idx] = None  # cycle guard - this format has none in practice
        if not isinstance(raw, str):
            result = raw
        elif raw.startswith("n|"):
            result = dec_b62(raw[2:])
        elif raw == "b|T":
            result = True
        elif raw == "b|F":
            result = False
        elif raw.startswith("a|"):
            # "a|" with nothing after the pipe is an EMPTY array - splitting
            # "a|"[2:] (="") on "|" gives [""], which would wrongly decode as
            # one ref (index 0) instead of zero. Guard explicitly.
            rest = raw[2:]
            refs = [dec_b62(x) for x in rest.split("|")] if rest else []
            result = [resolve(r) for r in refs]
        elif raw.startswith("o|"):
            rest = raw[2:]
            refs = [dec_b62(x) for x in rest.split("|")] if rest else []
            shape_idx = refs[0]
            shape_raw = flat[shape_idx]
            shape_rest = shape_raw[2:]
            key_idxs = [dec_b62(x) for x in shape_rest.split("|")] if shape_rest else []
            keys = [flat[ki] for ki in key_idxs]
            obj: dict[str, Any] = {}
            for i, key in enumerate(keys):
                obj[key] = resolve(refs[i + 1])
            result = obj
        else:
            result = raw
        cache[idx] = result
        return result

    return resolve(root_idx)


class Encoder:
    """Mirror-image of `decode_flat`: builds a fresh `flat` array (+ root ref)
    from a nested Python structure, preserving object-identity sharing (two
    dict/list values that are the SAME Python object get a single flat slot,
    referenced twice) and reusing one shape entry per distinct, ordered key
    set (matching how the real app's own encoder dedups repeated item/status
    shapes)."""

    def __init__(self) -> None:
        self.flat: list[Any] = []
        self._ref_of_id: dict[int, int] = {}
        self._keepalive: dict[int, Any] = {}  # keeps ids from being reused by the GC mid-encode
        self._shape_of_keys: dict[tuple[str, ...], int] = {}
        self._literal_index: dict[str, int] = {}

    def _literal_ref(self, token: str) -> int:
        if token in self._literal_index:
            return self._literal_index[token]
        idx = len(self.flat)
        self.flat.append(token)
        self._literal_index[token] = idx
        return idx

    def encode(self, value: Any) -> int:
        if value is None:
            return self._literal_ref("")
        if isinstance(value, bool):
            return self._literal_ref("b|T" if value else "b|F")
        if isinstance(value, str):
            return self._literal_ref(value)
        if isinstance(value, int):
            return self._literal_ref("n|" + enc_b62(value))
        if isinstance(value, list):
            key = id(value)
            if key in self._ref_of_id:
                return self._ref_of_id[key]
            idx = len(self.flat)
            self.flat.append(None)
            self._ref_of_id[key] = idx
            self._keepalive[key] = value
            refs = [self.encode(el) for el in value]
            self.flat[idx] = "a|" + "|".join(enc_b62(r) for r in refs)
            return idx
        if isinstance(value, dict):
            key = id(value)
            if key in self._ref_of_id:
                return self._ref_of_id[key]
            keys = list(value.keys())
            sig = tuple(keys)
            if sig in self._shape_of_keys:
                shape_idx = self._shape_of_keys[sig]
            else:
                key_refs = [self._literal_ref(k) for k in keys]
                shape_idx = len(self.flat)
                self.flat.append("a|" + "|".join(enc_b62(r) for r in key_refs))
                self._shape_of_keys[sig] = shape_idx
            idx = len(self.flat)
            self.flat.append(None)
            self._ref_of_id[key] = idx
            self._keepalive[key] = value
            value_refs = [self.encode(value[k]) for k in keys]
            self.flat[idx] = "o|" + "|".join(enc_b62(r) for r in [shape_idx] + value_refs)
            return idx
        raise TypeError(f"Unsupported value type for checklist encoding: {type(value)!r}")


def decode_shard_payload(raw_data: str) -> tuple[list[Any], Any]:
    """Parse one shard property's `data` string (itself a JSON string) into
    `(flat, root)` where `root` is the fully decoded Python structure."""
    parsed = json.loads(raw_data)
    flat, root_token = parsed[0], parsed[1]
    root_idx = dec_b62(root_token)
    return flat, decode_flat(flat, root_idx)


def encode_shard_payload(root: Any) -> tuple[str, int]:
    """Encode `root` back into a shard property `data` string, verifying the
    result decodes back to an identical structure before returning it.
    Returns `(payload_json_string, payload_length)`. Raises ValueError if the
    self-check fails - callers must never write an unverified payload."""
    encoder = Encoder()
    root_idx = encoder.encode(root)
    verify = decode_flat(encoder.flat, root_idx)
    if json.dumps(root, sort_keys=True) != json.dumps(verify, sort_keys=True):
        raise ValueError("Checklist re-encode failed self-verification (round trip mismatch) - refusing to write")
    payload = json.dumps([encoder.flat, enc_b62(root_idx)])
    return payload, len(payload)
