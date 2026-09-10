"""Fail-closed bounded reader for initial WebUI session-tail loads.

The reader never calls :meth:`Session.load` or ``read_source_text``.  Its
supported vertical slice is an inactive, ordinary WebUI sidecar whose metadata
prefix starts a top-level ``messages`` array whose closing bracket is visible in
either the fixed prefix or fixed file tail.  The active ``state.db`` row set is
converted with the authoritative projection and merged with the same
append-only reconciler as a full display load; if that exact proof cannot fit
its fixed read budget, the unchanged full reader runs after this bounded
reservation has been released.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import quote


BOUNDED_METADATA_PREFIX_BYTES = 8 * 1024 * 1024
BOUNDED_SIDECAR_TAIL_BYTES = 4 * 1024 * 1024
BOUNDED_STATE_DB_BYTES = 4 * 1024 * 1024
BOUNDED_READ_UPPER = (
    BOUNDED_METADATA_PREFIX_BYTES
    + BOUNDED_SIDECAR_TAIL_BYTES
    + BOUNDED_STATE_DB_BYTES
)
# Fixed estimate: 16 MiB physical reads * 12 expansion plus the legacy
# 1.5 MiB output allowance.  Reducing only this number would not make a full
# Session.load safe; every uncertain case below must instead leave this path.
BOUNDED_GATE_COST = BOUNDED_READ_UPPER * 12 + 1_572_864


class BoundedTailUnsupported(Exception):
    """The bounded reader cannot prove exact full-load semantics."""


@dataclass(frozen=True)
class BoundedTailRead:
    session: object
    message_count: int
    source_stamp: tuple[int, int, int]
    read_bounds: tuple[tuple[int, int], tuple[int, int]]
    state_db_bound: tuple[int, int] | None
    display_messages: list
    display_base_offset: int
    merged_message_count: int


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _stable_fd_stat(stat_before, stat_after) -> bool:
    return _stat_signature(stat_before) == _stat_signature(stat_after)


def _reject_json_constant(value: str):
    raise ValueError(f"unsupported JSON constant: {value}")


_JSON_DECODER = json.JSONDecoder(parse_constant=_reject_json_constant)


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _decode_json_value(text: str, index: int):
    try:
        return _JSON_DECODER.raw_decode(text, index)
    except ValueError as error:
        raise BoundedTailUnsupported("malformed JSON value") from error


def _scan_json_object(text: str) -> tuple[dict, list[tuple[str, int, int]]]:
    """Scan one complete JSON object while retaining member order.

    ``raw_decode`` parses each member value structurally.  We retain every key
    occurrence so duplicate top-level members are rejected rather than following
    Python's last-writer-wins behavior.
    """
    size = len(text)
    i = _skip_ws(text, 0)
    if i >= size or text[i] != "{":
        raise BoundedTailUnsupported("sidecar member is not a JSON object")
    i += 1
    values: dict[str, object] = {}
    members: list[tuple[str, int, int]] = []
    while True:
        i = _skip_ws(text, i)
        if i < size and text[i] == "}":
            i += 1
            break
        if i >= size or text[i] != '"':
            raise BoundedTailUnsupported("malformed JSON object member")
        key_start = i
        try:
            key, i = _JSON_DECODER.raw_decode(text, i)
        except ValueError as error:
            raise BoundedTailUnsupported("malformed JSON member name") from error
        key_end = i
        i = _skip_ws(text, i)
        if i >= size or text[i] != ":":
            raise BoundedTailUnsupported("missing JSON member separator")
        i = _skip_ws(text, i + 1)
        value_start = i
        value, i = _decode_json_value(text, i)
        if not isinstance(key, str):
            raise BoundedTailUnsupported("non-string JSON member name")
        values[key] = value
        members.append((key, key_start, value_start))
        i = _skip_ws(text, i)
        if i < size and text[i] == ",":
            i += 1
            continue
        if i < size and text[i] == "}":
            i += 1
            break
        raise BoundedTailUnsupported("malformed JSON object boundary")
    if _skip_ws(text, i) != size:
        raise BoundedTailUnsupported("trailing data after JSON object")
    return values, members


def _skip_json_value(text: str, index: int) -> int:
    """Lex one JSON value without retaining its parsed contents."""
    _, end = _decode_json_value(text, index)
    return end


def _skip_ws_backward(raw: bytes, index: int) -> int:
    while index >= 0 and raw[index : index + 1] in b" \t\r\n":
        index -= 1
    return index


def _skip_json_string_backward(raw: bytes, closing_quote: int) -> int:
    """Return the opening quote for the JSON string closed at ``closing_quote``.

    Escapes are handled by counting immediately preceding backslashes.  The
    scanner is deliberately byte-based: a fixed file-tail window may begin in
    the middle of a multi-byte UTF-8 code point, while individual JSON members
    are decoded only after their complete byte spans are known.
    """
    if closing_quote < 0 or raw[closing_quote : closing_quote + 1] != b'"':
        raise BoundedTailUnsupported("malformed JSON string boundary")
    index = closing_quote - 1
    while index >= 0:
        if raw[index] != 0x22:
            index -= 1
            continue
        escapes = 0
        cursor = index - 1
        while cursor >= 0 and raw[cursor] == 0x5C:
            escapes += 1
            cursor -= 1
        if escapes % 2 == 0:
            return index
        index -= 1
    raise BoundedTailUnsupported("unterminated JSON string in sidecar tail")


def _decode_json_bytes(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BoundedTailUnsupported("sidecar JSON member is not valid UTF-8") from error
    value, end = _decode_json_value(text, 0)
    if _skip_ws(text, end) != len(text):
        raise BoundedTailUnsupported("trailing data in JSON member")
    return value


def _find_messages_array_close_in_tail(tail_raw: bytes) -> int:
    """Locate the top-level ``messages`` close using a bounded reverse walk.

    The tail can contain the end of the array followed by arbitrary top-level
    members.  Reverse depth walking therefore has to identify each trailing
    member's key, not merely return the first direct ``]``: an array-valued
    member such as ``context_messages`` also closes at top-level depth.
    """
    index = _skip_ws_backward(tail_raw, len(tail_raw) - 1)
    if index < 0 or tail_raw[index] != 0x7D:  # }
        raise BoundedTailUnsupported("sidecar tail does not end with an object")
    index -= 1

    while True:
        index = _skip_ws_backward(tail_raw, index)
        if index < 0:
            raise BoundedTailUnsupported("truncated trailing sidecar member")
        byte = tail_raw[index]
        if byte == 0x5D:
            # The caller reaches here only after the bounded prefix scanner has
            # proven that the current top-level member is ``messages`` and its
            # opening bracket lies before this disjoint tail.  At a top-level
            # value boundary (the root itself, or after consuming a complete
            # trailing member and its comma), this direct array close is the
            # boundary we need.  Walking the whole array backward to recover its
            # unseen opening would instead fail on a string sliced at tail start;
            # row scanning below performs that bounded suffix proof separately.
            return index
        value_start = index
        if byte == 0x22:  # closing quote of a scalar string value
            value_start = _skip_json_string_backward(tail_raw, index)
        elif byte in (0x7D, 0x5D):  # } or ]
            value_close = index
            depth = 1
            index -= 1
            while True:
                index = _skip_ws_backward(tail_raw, index)
                if index < 0:
                    raise BoundedTailUnsupported("unbalanced trailing JSON value")
                token = tail_raw[index]
                if token == 0x22:
                    index = _skip_json_string_backward(tail_raw, index) - 1
                    continue
                if token in (0x7D, 0x5D):
                    depth += 1
                elif token in (0x7B, 0x5B):
                    depth -= 1
                    if depth == 0:
                        value_start = index
                        break
                # Commas and colons are ordinary bytes inside a nested object
                # or array.  String spans above are the only scalar context in
                # which they require special treatment.
                index -= 1
            if tail_raw[value_start] == 0x5B and tail_raw[value_close] == 0x5D:
                pass
            elif not (
                tail_raw[value_start] == 0x7B
                and tail_raw[value_close] == 0x7D
            ):
                raise BoundedTailUnsupported("mismatched JSON container in tail")
        else:
            # Scalar numbers and the three lowercase JSON literals cannot
            # contain ':', so scanning to that separator lexes the value.
            while index >= 0 and tail_raw[index] not in b": \t\r\n":
                if tail_raw[index] in b'{}[]",':
                    raise BoundedTailUnsupported("malformed JSON scalar in tail")
                index -= 1
            value_start = index + 1
            if index < 0 or tail_raw[index] != 0x3A:
                raise BoundedTailUnsupported("malformed trailing JSON scalar")
            scalar = tail_raw[value_start : _skip_ws_backward(tail_raw, index - 1) + 1]
            if scalar not in (b"true", b"false", b"null") and not _is_json_number_bytes(scalar):
                raise BoundedTailUnsupported("malformed JSON scalar in tail")

        index = _skip_ws_backward(tail_raw, value_start - 1)
        if index < 0 or tail_raw[index] != 0x3A:  # :
            raise BoundedTailUnsupported("missing trailing member separator")
        index = _skip_ws_backward(tail_raw, index - 1)
        if index < 0 or tail_raw[index] != 0x22:
            raise BoundedTailUnsupported("malformed trailing member name")
        key_close = index
        key_open = _skip_json_string_backward(tail_raw, key_close)
        key = _decode_json_bytes(tail_raw[key_open : key_close + 1])
        if not isinstance(key, str):
            raise BoundedTailUnsupported("non-string trailing member name")
        if key == "messages":
            if tail_raw[value_start] != 0x5B or tail_raw[value_close] != 0x5D:
                raise BoundedTailUnsupported("messages member is not an array")
            return value_close

        index = _skip_ws_backward(tail_raw, key_open - 1)
        if index < 0 or tail_raw[index] != 0x2C:  # ,
            raise BoundedTailUnsupported("malformed trailing object boundary")
        index -= 1


def _is_json_number_bytes(raw: bytes) -> bool:
    if not raw:
        return False
    # JSON's lexical grammar, not Python's permissive float() parser.  In
    # particular NaN, Infinity, leading plus signs, and trailing whitespace
    # cannot participate in a structural proof.
    return re.fullmatch(
        rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", raw
    ) is not None


def _scan_array_rows_backward(
    tail_raw: bytes, array_close: int, *, max_rows: int
) -> tuple[list[dict], bool]:
    """Decode at most the final ``max_rows`` complete top-level array rows."""
    if max_rows <= 0:
        raise BoundedTailUnsupported("bounded row scan limit is invalid")
    index = _skip_ws_backward(tail_raw, array_close - 1)
    rows: list[dict] = []
    reached_opening = False
    while index >= 0:
        if tail_raw[index] == 0x5B:  # [
            reached_opening = True
            break
        if tail_raw[index] == 0x2C:  # separating comma before the closing bracket
            index = _skip_ws_backward(tail_raw, index - 1)
            if index < 0:
                raise BoundedTailUnsupported("truncated messages array boundary")
            if tail_raw[index] == 0x5B:
                reached_opening = True
                break
        if tail_raw[index] != 0x7D:  # closing brace of an array row
            raise BoundedTailUnsupported("messages member is not an object array")

        # Reverse-lex one complete object. Strings are opaque; nested containers
        # maintain depth. Scalar bytes (numbers, literals, commas, and colons)
        # cannot affect object boundary tracking outside strings/containers.
        row_end = index
        depth = 1
        cursor = row_end - 1
        row_start: int | None = None
        while cursor >= 0:
            token = tail_raw[cursor]
            if token == 0x22:
                try:
                    string_open = _skip_json_string_backward(tail_raw, cursor)
                except BoundedTailUnsupported as error:
                    if str(error) != "unterminated JSON string in sidecar tail":
                        raise
                    break
                cursor = string_open - 1
                continue
            if token in (0x7D, 0x5D):  # } or ]
                depth += 1
            elif token in (0x7B, 0x5B):  # { or [
                depth -= 1
                if depth == 0:
                    if token != 0x7B:
                        raise BoundedTailUnsupported("unbalanced messages array row")
                    row_start = cursor
                    break
                if depth < 0:
                    raise BoundedTailUnsupported("unbalanced messages array row")
            cursor -= 1
        if row_start is None:
            # A disjoint physical tail may begin in the middle of its first row.
            # That row is deliberately discarded; only complete later rows are
            # returned, and the caller proves that the retained suffix is enough.
            break

        row = _decode_json_bytes(tail_raw[row_start : row_end + 1])
        if not isinstance(row, dict):
            raise BoundedTailUnsupported("messages member is not an object array")
        rows.append(row)
        if len(rows) >= max_rows:
            break

        index = _skip_ws_backward(tail_raw, row_start - 1)
        if index < 0:
            raise BoundedTailUnsupported("truncated messages array boundary")
        if tail_raw[index] == 0x5B:
            reached_opening = True
            break
        if tail_raw[index] != 0x2C:
            raise BoundedTailUnsupported("malformed messages array boundary")
        index = _skip_ws_backward(tail_raw, index - 1)
        if index < 0:
            raise BoundedTailUnsupported("truncated messages array boundary")
    rows.reverse()
    if reached_opening and not rows:
        # A one-row array reaches ``[`` without seeing a top-level comma.  An
        # empty array takes the same path and is represented by [] below.
        body = tail_raw[index + 1 : array_close]
        if any(byte not in b" \t\r\n" for byte in body):
            row = _decode_json_bytes(body)
            if not isinstance(row, dict):
                raise BoundedTailUnsupported("messages member is not an object array")
            rows.append(row)
    return rows, reached_opening


def _find_messages_member(prefix_text: str):
    """Find the structural top-level ``messages`` member in the 8 MiB prefix."""
    decoder = _JSON_DECODER
    size = len(prefix_text)
    i = _skip_ws(prefix_text, 0)
    if i >= size or prefix_text[i] != "{":
        raise BoundedTailUnsupported("sidecar is not a JSON object")
    i += 1
    seen: set[str] = set()
    while True:
        i = _skip_ws(prefix_text, i)
        if i < size and prefix_text[i] == "}":
            raise BoundedTailUnsupported("top-level messages array is absent")
        if i >= size or prefix_text[i] != '"':
            raise BoundedTailUnsupported("truncated top-level member name")
        key_start = i
        try:
            key, i = decoder.raw_decode(prefix_text, i)
        except ValueError as error:
            raise BoundedTailUnsupported("truncated or malformed member name") from error
        if not isinstance(key, str):
            raise BoundedTailUnsupported("non-string top-level member name")
        if key in seen:
            raise BoundedTailUnsupported("duplicate top-level metadata member")
        seen.add(key)
        i = _skip_ws(prefix_text, i)
        if i >= size or prefix_text[i] != ":":
            raise BoundedTailUnsupported("truncated top-level member separator")
        i = _skip_ws(prefix_text, i + 1)
        if key == "messages":
            if i >= size or prefix_text[i] != "[":
                raise BoundedTailUnsupported("messages member is not an array")
            return key_start, i, seen
        i = _skip_json_value(prefix_text, i)
        i = _skip_ws(prefix_text, i)
        if i < size and prefix_text[i] == ",":
            i += 1
            continue
        if i < size and prefix_text[i] == "}":
            raise BoundedTailUnsupported("top-level messages array is absent")
        raise BoundedTailUnsupported("truncated top-level object boundary")


def _decode_prefix(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        # A code point sliced exactly at the 8 MiB cap is an unsupported normal
        # boundary. Any other malformed UTF-8 is likewise fail-closed.
        if error.reason == "unexpected end of data" and error.end == len(raw):
            return raw[: error.start].decode("utf-8")
        raise BoundedTailUnsupported("sidecar metadata is not valid UTF-8")


def _byte_offset(text: str, char_offset: int) -> int:
    return len(text[:char_offset].encode("utf-8"))


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _is_safe_default(value) -> bool:
    return value is None or value is False or value == 0 or value == "" or value == [] or value == {}


def _reject_uncertain_metadata(metadata: dict, sid: str) -> None:
    required = {"session_id", "title", "created_at", "updated_at", "message_count"}
    if not required.issubset(metadata):
        raise BoundedTailUnsupported("incomplete sidecar metadata")
    if metadata["session_id"] != sid or not isinstance(metadata["title"], str):
        raise BoundedTailUnsupported("invalid sidecar identity metadata")
    if type(metadata["message_count"]) is not int or metadata["message_count"] < 0:
        raise BoundedTailUnsupported("invalid sidecar message count")
    if not _finite_number(metadata["created_at"]) or not _finite_number(metadata["updated_at"]):
        raise BoundedTailUnsupported("uncertain sidecar timestamp")

    # Empty/default markers are harmless. Every nonempty or nonzero marker can
    # change full-load merge, recovery, lineage, source, or truncation behavior.
    for field in (
        "active_stream_id",
        "pending_user_message",
        "parent_session_id",
        "pre_compression_snapshot",
        "compression_anchor_visible_idx",
        "compression_anchor_message_key",
        "compression_anchor_summary",
        "compression_recovery",
        "recommended_recovery_action",
        "compression_recovery_source_session_id",
        "compression_recovery_action",
        "clear_generation",
        "intentional_shrink_generation",
        "is_cli_session",
        "read_only",
        "session_source",
        "source_tag",
        "raw_source",
        "source_label",
    ):
        if not _is_safe_default(metadata.get(field)):
            raise BoundedTailUnsupported(f"unsupported session field: {field}")
    # Zero can itself be a meaningful watermark/generation, so these fields must
    # be absent or null rather than merely falsey.
    for field in (
        "truncation_watermark",
        "truncation_boundary",
    ):
        if metadata.get(field) is not None:
            raise BoundedTailUnsupported(f"unsupported session field: {field}")


def _database_path(profile):
    from api.models import _active_state_db_path, _get_profile_home

    if isinstance(profile, str) and profile:
        return _get_profile_home(profile) / "state.db"
    return Path(_active_state_db_path())


def _path_signature(path: Path):
    try:
        return _stat_signature(path.stat())
    except FileNotFoundError:
        return None
    except OSError as error:
        raise BoundedTailUnsupported("state.db stat failed") from error


def _assert_indexed_messages_lookup(conn: sqlite3.Connection, where: str, sid: str) -> None:
    plan = "\n".join(
        " ".join(str(part) for part in row) for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT 1 FROM messages WHERE {where} LIMIT 1", (sid,)
        )
    ).lower()
    if "scan messages" in plan or not (
        "using index" in plan or "using covering index" in plan
    ):
        raise BoundedTailUnsupported("state.db lacks an indexed session lookup")


def _read_state_db_messages(sid: str, profile) -> tuple[list, tuple[int, int] | None]:
    """Read and convert the exact active state.db set within its fixed cap.

    This is deliberately not a row-count heuristic.  It selects the same columns
    as ``api.models.get_state_db_session_messages``, measures their textual
    bytes (plus fixed row overhead) before ``fetchall``, converts them with the
    same projection rules, and feeds the result to the authoritative merge in
    the caller.  Missing databases remain authoritatively empty.
    """
    from api.models import _json_loads_if_string
    from api.wsbound import (
        MemoryBudgetExceeded,
        READ_BUDGET,
        ReadBudget,
        reserve_sql_rows,
    )

    path = _database_path(profile)
    try:
        if not path.exists():
            return [], None
        db_before = _path_signature(path)
        wal_before = _path_signature(path.with_name(path.name + "-wal"))
        shm_before = _path_signature(path.with_name(path.name + "-shm"))
        uri = quote(str(path.resolve()), safe="/")
        # The state proof gets its own hard 4 MiB row-materialization budget.
        # It is intentionally a ContextVar switch, not another admission gate.
        state_token = READ_BUDGET.set(ReadBudget(BOUNDED_STATE_DB_BYTES))
        try:
            with closing(
                sqlite3.connect(f"file:{uri}?mode=ro", uri=True, timeout=2.0)
            ) as conn:
                conn.execute("PRAGMA query_only=ON")
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                rows = conn.execute("PRAGMA table_info(messages)").fetchall()
                columns = {str(row["name"]) for row in rows}
                required = {"session_id", "role", "content", "timestamp"}
                if not {"id", *required}.issubset(columns):
                    raise BoundedTailUnsupported("state.db messages schema is unsupported")
                known_optional = {
                    "id", "active", "tool_call_id", "tool_calls", "tool_name",
                    "reasoning", "reasoning_details", "codex_reasoning_items",
                    "reasoning_content", "codex_message_items", "api_content",
                }
                if not columns.issubset(required | known_optional):
                    raise BoundedTailUnsupported("state.db messages schema is unsupported")
                active_clause = ""
                if "active" in columns:
                    active_clause = " AND (active IS NULL OR active != 0)"
                where = f"session_id = ?{active_clause}"
                _assert_indexed_messages_lookup(conn, where, str(sid))
                optional = [
                    "tool_call_id", "tool_calls", "tool_name", "reasoning",
                    "reasoning_details", "codex_reasoning_items",
                    "reasoning_content", "codex_message_items", "api_content",
                ]
                optional = [column for column in optional if column in columns]
                selected = ["id", "role", "content", "timestamp"] + optional
                # Measure before fetch: a hostile/oversized row must exhaust the
                # 4 MiB proof rather than be materialized. Empty is exact row-set
                # evidence, not a count-only substitute for loaded rows.
                reserve_sql_rows(conn, selected + ["session_id"], where, (str(sid),))
                raw_rows = conn.execute(
                    f"SELECT {', '.join(selected)}, session_id FROM messages "
                    f"WHERE {where} ORDER BY id ASC",
                    (str(sid),),
                ).fetchall()
                msgs = []
                for row in raw_rows:
                    durable_id = row["id"]
                    if isinstance(durable_id, bool) or not isinstance(durable_id, int):
                        raise BoundedTailUnsupported("state.db durable row identity is invalid")
                    msg = {
                        "role": row["role"],
                        "content": row["content"],
                        "timestamp": row["timestamp"],
                    }
                    for column in optional:
                        value = row[column]
                        if value in (None, ""):
                            continue
                        if column in {
                            "tool_calls", "reasoning_details",
                            "codex_reasoning_items", "codex_message_items",
                        }:
                            value = _json_loads_if_string(value)
                        msg[column] = value
                    if isinstance(msg.get("api_content"), str) and msg["api_content"]:
                        msg["_state_db_row_id"] = durable_id
                    if msg.get("role") == "tool" and msg.get("tool_name") and not msg.get("name"):
                        msg["name"] = msg["tool_name"]
                    msgs.append(msg)
                conn.commit()
        finally:
            READ_BUDGET.reset(state_token)
    except BoundedTailUnsupported:
        raise
    except (Exception, MemoryBudgetExceeded) as error:
        raise BoundedTailUnsupported("state.db bounded read failed") from error

    if (
        _path_signature(path) != db_before
        or _path_signature(path.with_name(path.name + "-wal")) != wal_before
        or _path_signature(path.with_name(path.name + "-shm")) != shm_before
    ):
        raise BoundedTailUnsupported("state.db changed during bounded read")
    return msgs, (len(msgs), BOUNDED_STATE_DB_BYTES)


def _reject_ambiguous_state_duplicates(state_messages: list) -> None:
    """Reject conflicting rows sharing the authoritative dedup key."""
    from api.models import _session_message_dedup_key

    seen: dict[object, str] = {}
    for row in state_messages:
        try:
            key = _session_message_dedup_key(row)
            identity = json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise BoundedTailUnsupported("uncertain state.db message identity") from error
        prior = seen.get(key)
        if prior is not None and prior != identity:
            raise BoundedTailUnsupported("ambiguous duplicate state.db rows")
        seen[key] = identity


def _latest_valid_todo_snapshot_index(messages: list) -> int | None:
    """Return the suffix's latest valid todo write, or fail closed."""
    from api.todo_state import parse_todo_tool_result

    for index in range(len(messages) - 1, -1, -1):
        row = messages[index]
        if not isinstance(row, dict) or row.get("role") != "tool":
            continue
        if parse_todo_tool_result(row.get("content")) is not None:
            return index
        # A malformed later todo-shaped write cannot exclude a valid write in
        # the unread prefix.  The caller must fall back in that case.
        content = row.get("content")
        if isinstance(content, str) and '"todos"' in content:
            return None
    return None


def _prove_partial_suffix_for_window(
    messages: list, *, message_count: int, msg_limit: int
) -> tuple[list, int]:
    """Return a suffix with full-source coordinates, or fail closed.

    This is not a second pagination implementation.  It invokes the route's
    authoritative display selector over the bounded trailing rows, then retains
    everything from the earlier of that window or the latest valid todo write.
    A hidden row before this suffix cannot affect a trailing 4,096-row display
    scan; it could still affect todo state, so absence of a valid suffix todo
    snapshot is unsupported rather than interpreted as ``None``.
    """
    from api.routes import (
        _message_counts_as_renderable_for_window,
        _message_window_for_display,
    )

    if len(messages) > 4096:
        raise BoundedTailUnsupported("bounded messages suffix exceeds scan limit")
    if message_count <= len(messages):
        raise BoundedTailUnsupported("partial sidecar suffix count is invalid")
    if any("_partial" in row for row in messages):
        raise BoundedTailUnsupported("partial message crosses sidecar suffix boundary")

    selected, selected_offset = _message_window_for_display(
        messages, msg_limit=msg_limit
    )
    renderable_count = sum(
        1 for row in selected if _message_counts_as_renderable_for_window(row)
    )
    if renderable_count < msg_limit:
        raise BoundedTailUnsupported("messages suffix does not contain requested window")

    todo_index = _latest_valid_todo_snapshot_index(messages)
    if todo_index is None:
        raise BoundedTailUnsupported("latest todo snapshot is outside sidecar suffix")
    keep_index = min(selected_offset, todo_index)
    suffix = messages[keep_index:]
    base_offset = message_count - len(messages) + keep_index
    if base_offset <= 0 or len(suffix) > 4096:
        raise BoundedTailUnsupported("partial sidecar source offset is invalid")
    return suffix, base_offset


def _reconcile_state_db_messages(
    sid: str, profile, sidecar_messages: list, *, partial_sidecar: bool
) -> tuple[list, tuple[int, int] | None]:
    state_messages, state_bound = _read_state_db_messages(sid, profile)
    if partial_sidecar and state_messages:
        raise BoundedTailUnsupported(
            "nonempty state.db cannot be aligned with an unread sidecar prefix"
        )
    if partial_sidecar:
        # Session.compact() also emits user_message_count by walking the
        # complete sidecar.  The persisted message_count cannot reconstruct the
        # hidden prefix's role distribution, so even an exactly empty state set
        # must use the full oracle rather than emit a different payload.
        raise BoundedTailUnsupported(
            "partial sidecar suffix cannot prove complete compact metadata"
        )
    if state_messages:
        _reject_ambiguous_state_duplicates(state_messages)
    from api.models import merge_session_messages_append_only

    # Calling the real reconciler (not a lookalike tail approximation) is the
    # equivalence proof.  We read the complete active row set, so replay
    # duplicates and null timestamps follow exactly the full-path behavior.
    merged = merge_session_messages_append_only(sidecar_messages, state_messages)
    return merged, state_bound


def read_bounded_session_tail(
    sid: str, *, expected_source=None, msg_limit: int
) -> BoundedTailRead:
    """Read an exact simple sidecar without constructing a full Session graph."""
    from api.config import SESSION_DIR
    from api.models import (
        Session,
        _collapse_adjacent_duplicate_partials,
        is_safe_session_id,
    )
    from api.wsbound import READ_BUDGET

    if not isinstance(sid, str) or not is_safe_session_id(sid):
        raise BoundedTailUnsupported("invalid bounded session id")
    if isinstance(msg_limit, bool) or not isinstance(msg_limit, int):
        raise BoundedTailUnsupported("invalid bounded message limit")
    msg_limit = max(1, min(msg_limit, 500))
    budget = READ_BUDGET.get()
    if budget is None:
        raise BoundedTailUnsupported("bounded read budget is absent")
    path = Path(SESSION_DIR) / f"{sid}.json"
    try:
        with path.open("rb") as source:
            stat_before = os.fstat(source.fileno())
            signature = _stat_signature(stat_before)
            stamp = (stat_before.st_ino, stat_before.st_mtime_ns, stat_before.st_size)
            if expected_source is not None and tuple(expected_source) != stamp:
                raise BoundedTailUnsupported("sidecar changed before bounded read")
            if stat_before.st_size <= 0:
                raise BoundedTailUnsupported("empty sidecar")

            prefix_len = min(BOUNDED_METADATA_PREFIX_BYTES, stat_before.st_size)
            prefix_raw = source.read(prefix_len)
            if not _stable_fd_stat(stat_before, os.fstat(source.fileno())):
                raise BoundedTailUnsupported("sidecar changed during metadata read")
            budget.consume(len(prefix_raw))

            tail_len = min(BOUNDED_SIDECAR_TAIL_BYTES, stat_before.st_size)
            tail_start = stat_before.st_size - tail_len
            source.seek(tail_start)
            tail_raw = source.read(tail_len)
            if (
                len(prefix_raw) != prefix_len
                or len(tail_raw) != tail_len
                or not _stable_fd_stat(stat_before, os.fstat(source.fileno()))
            ):
                raise BoundedTailUnsupported("sidecar changed during bounded read")
            budget.consume(len(tail_raw))
    except BoundedTailUnsupported:
        raise
    except Exception as error:
        raise BoundedTailUnsupported("sidecar bounded open/read failed") from error

    prefix_text = _decode_prefix(prefix_raw)
    marker_char, value_char, pre_names = _find_messages_member(prefix_text)
    marker_bytes = _byte_offset(prefix_text, marker_char)
    value_bytes = _byte_offset(prefix_text, value_char)
    if value_bytes >= len(prefix_raw):
        raise BoundedTailUnsupported("messages value starts outside prefix cap")

    prefix_object = prefix_text[:marker_char].rstrip()
    if prefix_object.endswith(","):
        prefix_object = prefix_object[:-1].rstrip()
    prefix_object += "\n}"
    metadata, prefix_members = _scan_json_object(prefix_object)
    if len({name for name, _, _ in prefix_members}) != len(prefix_members):
        raise BoundedTailUnsupported("duplicate top-level metadata member")
    if not isinstance(metadata, dict):
        raise BoundedTailUnsupported("sidecar metadata is not an object")

    # Establish the complete array boundary before trusting any rows.  L1 is
    # a complete array in the prefix; L2 starts in the prefix and closes in the
    # fixed file tail.  An end in the unread middle is never guessable.
    try:
        decoded_array, decoded_array_end = _decode_json_value(prefix_text, value_char)
    except BoundedTailUnsupported:
        decoded_array = None
        decoded_array_end = None

    if decoded_array_end is not None:
        array_close = _byte_offset(prefix_text, decoded_array_end) - 1
        if array_close >= len(prefix_raw):
            raise BoundedTailUnsupported("bounded array close spans decoder cap")
        messages = decoded_array
        reached_opening = True
        if array_close >= tail_start:
            post_array_raw = tail_raw[array_close - tail_start + 1 :]
        elif tail_start <= len(prefix_raw):
            # Prefix and tail overlap (or abut), so their union contains every
            # byte after the array without a third read.
            post_array_raw = prefix_raw[array_close + 1 : tail_start] + tail_raw
        else:
            raise BoundedTailUnsupported("post-messages metadata crosses unread gap")
    else:
        array_close_relative = _find_messages_array_close_in_tail(tail_raw)
        array_close = tail_start + array_close_relative
        if array_close < tail_start or array_close >= stat_before.st_size:
            raise BoundedTailUnsupported("bounded messages array close is invalid")
        if array_close < tail_start:
            raise BoundedTailUnsupported("messages array close is outside tail window")
        messages, reached_opening = _scan_array_rows_backward(
            tail_raw,
            array_close_relative,
            max_rows=4096,
        )
        post_array_raw = tail_raw[array_close_relative + 1 :]

    if not isinstance(messages, list) or not all(isinstance(row, dict) for row in messages):
        raise BoundedTailUnsupported("messages member is not an object array")
    try:
        probe_text = b'{"_bounded_tail_probe":0' + post_array_raw
        probe, probe_members = _scan_json_object(probe_text.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise BoundedTailUnsupported("sidecar tail is not valid UTF-8") from error
    if len({name for name, _, _ in probe_members}) != len(probe_members):
        raise BoundedTailUnsupported("duplicate top-level sidecar member")
    if probe_members[0][0] != "_bounded_tail_probe":
        raise BoundedTailUnsupported("bounded probe layout changed")
    tail_members = {name: value for name, value in probe.items() if name != "_bounded_tail_probe"}
    if "messages" in tail_members or set(metadata).intersection(tail_members):
        raise BoundedTailUnsupported("duplicate pre/post messages member")
    metadata.update(tail_members)
    parsed_pre_names = {name for name, _, _ in prefix_members}
    parsed_post_names = set(tail_members)
    structural_pre_names = pre_names - {"messages"}
    if parsed_pre_names != structural_pre_names or set(metadata) != (
        parsed_pre_names | parsed_post_names
    ):
        # The structural scanner and parsed members must describe the same
        # top-level layout; parser drift is always unsupported, not guessed.
        raise BoundedTailUnsupported("bounded metadata layout mismatch")

    _reject_uncertain_metadata(metadata, sid)
    count = metadata["message_count"]
    display_base_offset = 0
    if not reached_opening:
        messages, display_base_offset = _prove_partial_suffix_for_window(
            messages,
            message_count=count,
            msg_limit=msg_limit,
        )
    # Match Session.load's duplicate-partial normalization, but never self-heal:
    # if normalization changes the authoritative array/count, use full load.
    messages, collapsed = _collapse_adjacent_duplicate_partials(messages)
    if collapsed or (reached_opening and len(messages) != count):
        raise BoundedTailUnsupported("sidecar message count does not match array")
    if any(not _finite_number(row.get("timestamp")) for row in messages):
        raise BoundedTailUnsupported("null or uncertain message timestamp")
    identities = []
    for row in messages:
        if any(
            key in row
            for key in (
                "_lineage_root_id",
                "_lineage_tip_id",
                "_compression_segment_count",
                "_compressionRecovery",
            )
        ):
            raise BoundedTailUnsupported("message carries lineage/compression metadata")
        try:
            identities.append(
                json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise BoundedTailUnsupported("uncertain message identity") from error
    if len(set(identities)) != len(identities):
        raise BoundedTailUnsupported("duplicate or ambiguous message identity")
    if not reached_opening:
        try:
            suffix_max_timestamp = max(float(row.get("timestamp")) for row in messages)
        except (TypeError, ValueError, OverflowError) as error:
            raise BoundedTailUnsupported("uncertain sidecar suffix timestamp") from error
        if float(metadata["updated_at"]) != suffix_max_timestamp:
            raise BoundedTailUnsupported("hidden sidecar timestamp may be newer")

    # Mark derived state before construction bookkeeping. save() must refuse it,
    # and no route may insert it into SESSIONS or any writer/merge cache.
    metadata["messages"] = messages
    try:
        session = Session(**metadata)
    except Exception as error:
        raise BoundedTailUnsupported("bounded metadata cannot construct Session") from error
    from api.models import _overlay_composer_draft_sidecar

    _overlay_composer_draft_sidecar(session)
    session.messages = list(messages)
    session._loaded_metadata_only = True
    session._metadata_message_count = count
    session._count_unavailable = False
    session._bounded_derived_read = True
    session._bounded_tail_base_offset = display_base_offset

    display_messages, state_db_bound = _reconcile_state_db_messages(
        sid,
        session.profile,
        messages,
        partial_sidecar=not reached_opening,
    )
    try:
        final_stat = path.stat()
    except OSError as error:
        raise BoundedTailUnsupported("final sidecar stat failed") from error
    if _stat_signature(final_stat) != signature:
        raise BoundedTailUnsupported("sidecar changed after bounded read")

    return BoundedTailRead(
        session=session,
        message_count=count,
        source_stamp=stamp,
        read_bounds=(
            (0, len(prefix_raw)),
            (tail_start, tail_start + len(tail_raw)),
        ),
        state_db_bound=state_db_bound,
        display_messages=display_messages,
        display_base_offset=display_base_offset,
        merged_message_count=(
            display_base_offset + len(display_messages)
            if not reached_opening
            else len(display_messages)
        ),
    )
