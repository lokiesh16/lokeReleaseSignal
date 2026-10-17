# -*- coding: utf-8 -*-
"""
API Chain Analyzer

Deterministic (non-AI) detector that scans a chronological sequence of
captured network requests/responses and finds where one call's response
value (e.g. an auth token, a created resource id) is reused in a later
call's request (header, body, or URL) - the data-flow "chain" that turns
a list of independent API calls into one functional scenario.

Detection is heuristic and best-effort: it only indexes values sourced
from RESPONSE bodies (a server-issued token/id feeding a later request).
A client-invented correlation value that never appears in any response
is out of scope. Results are meant to be reviewed/edited by the user
before being used to generate a test script, not trusted blindly.
"""

import json
import re
from urllib.parse import urlsplit

MAX_LIST_ITEMS = 20
MAX_ENTRIES = 40
MIN_STRING_LEN = 4
MIN_SUBSTRING_LEN = 4
LARGE_NUMBER_DIGITS = 5

STOPLIST = {
    "true", "false", "null", "none", "active", "inactive", "pending",
    "success", "error", "ok", "yes", "no", "unknown", "male", "female",
    "admin", "user", "guest", "created", "updated", "deleted", "enabled",
    "disabled", "application/json", "utf-8", "get", "post", "put", "delete",
    "patch",
}

ID_KEY_PATTERN = re.compile(r'(^id$|_id$|Id$|ID$|_uuid$|Uuid$|uuid$|token|Token|_key$|Key$)')


def _safe_json_loads(body):
    """Parse a (possibly truncated) captured body. Never raises."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def flatten_json(obj, prefix=""):
    """Flatten a dict/list JSON body into {dotted.path: scalar_leaf}."""
    out = {}

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node[:MAX_LIST_ITEMS]):
                walk(v, f"{path}[{i}]")
        else:
            if node is not None and node != "":
                out[path] = node

    walk(obj, prefix)
    return out


def _leaf_key(path):
    """Best-effort field-name extraction from a flattened path, e.g. 'data[0].id' -> 'id'."""
    last = path.rsplit(".", 1)[-1]
    return last.split("[")[0]


def is_candidate_value(key, value):
    """Filter noise out before a response value is eligible to be chained."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        s = value.strip()
        if len(s) < MIN_STRING_LEN:
            return False
        if s.lower() in STOPLIST:
            return False
        return True
    if isinstance(value, (int, float)):
        if ID_KEY_PATTERN.search(key or ""):
            return True
        return abs(value) >= 10 ** (LARGE_NUMBER_DIGITS - 1)
    return False


def _extract_common_origin(entries):
    """Shared scheme+host across entries, so it can be stripped before URL substring checks."""
    origins = set()
    for e in entries:
        parts = urlsplit(e.get("url") or "")
        if parts.scheme and parts.netloc:
            origins.add(f"{parts.scheme}://{parts.netloc}")
    if len(origins) == 1:
        return next(iter(origins))
    return ""


def _important_headers(headers):
    """Keep only headers likely to carry chained values; drop cookie/UA/sec-fetch-* noise."""
    if not headers:
        return {}
    keep = {}
    for k, v in headers.items():
        lk = (k or "").lower()
        if lk == "content-type" or lk == "authorization" or lk.startswith("x-"):
            keep[k] = v
    return keep


def detect_api_chains(entries):
    """
    entries: list of dicts with keys url, method, requestHeaders, requestBody,
             responseBody, status, ts (epoch ms). Order is not trusted - the
             entries are re-sorted by `ts` internally.

    Returns {"steps": [{step, method, url, status, ts}, ...],
             "links": [{from_step, from_path, to_step, location,
                        location_detail, value_preview}, ...]}.
    """
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"Too many entries ({len(entries)}); max is {MAX_ENTRIES}")

    ordered = sorted(entries, key=lambda e: e.get("ts") or 0)
    origin = _extract_common_origin(ordered)

    steps = []
    links = []
    seen_pairs = set()
    value_index = []  # list of (value_str, source_step, source_path)

    for i, entry in enumerate(ordered):
        url = entry.get("url") or ""
        url_remainder = url[len(origin):] if origin and url.startswith(origin) else url

        req_body_obj = _safe_json_loads(entry.get("requestBody"))
        req_flat = flatten_json(req_body_obj) if req_body_obj is not None else {}
        req_headers = _important_headers(entry.get("requestHeaders"))

        for value_str, src_step, src_path in value_index:
            pair_key = (src_step, i, value_str)
            if pair_key in seen_pairs:
                continue

            location = None
            location_detail = None

            # Substring checks (auth headers wrap the raw value, e.g. "Token token=<value>";
            # URLs embed ids in the path) - require a reasonable min length to avoid
            # a short/common value matching everything.
            if len(value_str) >= MIN_SUBSTRING_LEN:
                for hname, hval in req_headers.items():
                    if value_str in str(hval):
                        location, location_detail = "header", hname
                        break
                if location is None and value_str in url_remainder:
                    location, location_detail = "url", url_remainder

            # Equality check against flattened request-body leaves - precise even
            # for short values (e.g. numeric ids), so no length gate here.
            if location is None:
                for path, v in req_flat.items():
                    if str(v) == value_str:
                        location, location_detail = "body", path
                        break

            if location:
                seen_pairs.add(pair_key)
                links.append({
                    "from_step": src_step,
                    "from_path": src_path,
                    "to_step": i,
                    "location": location,
                    "location_detail": location_detail,
                    "value_preview": value_str[:40],
                })

        resp_obj = _safe_json_loads(entry.get("responseBody"))
        if resp_obj is not None:
            for path, v in flatten_json(resp_obj).items():
                if is_candidate_value(_leaf_key(path), v):
                    value_index.append((str(v), i, path))

        steps.append({
            "step": i,
            "method": entry.get("method"),
            "url": url,
            "status": entry.get("status"),
            "ts": entry.get("ts"),
        })

    return {"steps": steps, "links": links}
