"""Bounded, checksum-verified artifact pages without Git or artifact writes."""
from __future__ import annotations

from itertools import islice
from pathlib import Path

import review_runtime

LIMIT = 24 * 1024


def view_continuation(context, artifact, pointer, offset, page_size=100, *, string_offset=None, scope="context"):
    value = {"command": "status" if scope == "runtime" else "review_session.py view", "artifact_scope": scope,
             "artifact": artifact, "pointer": pointer, "offset": offset, "page_size": page_size}
    if string_offset is not None:
        value["string_offset"] = string_offset
    context = Path(context)
    if context.parent.name in ("sessions", "responses"):
        value.update(runtime_root=str(context.parent.parent), request_id=context.name)
    return value


def artifact_view(context, artifact, pointer="", offset=0, page_size=100, string_offset=0,
                  scope="context", *, cache=None):
    key = (str(context), artifact)
    if cache is not None and key in cache:
        value = cache[key]
    else:
        value = review_runtime.load_view(context, artifact)
        if cache is not None:
            cache[key] = value
    if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
        raise ValueError("view pointer must be a JSON pointer.")
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdecimal():
                raise ValueError("view array pointer needs a nonnegative index")
            value = value[int(token)]
        else:
            value = value[token]
    if type(offset) is not int or offset < 0 or type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid view page.")
    if type(string_offset) is not int or string_offset < 0:
        raise ValueError("Invalid string_offset.")
    if isinstance(value, (list, dict)) and offset > len(value):
        raise ValueError("view offset exceeds the selected collection")

    def continuation(place, start=0, **options):
        return view_continuation(context, artifact, place, start, page_size, scope=scope, **options)

    def preview(item, place, start=0, field_limit=24):
        size = len(review_runtime.canonical(item))
        if size <= LIMIT // 4 and not start:
            return item, []
        if isinstance(item, str):
            text = item[:2048]
            link = continuation(place, string_offset=len(text))
            return {"omitted": True, "characters": len(item), "preview": text, "continue": link}, [link]
        if isinstance(item, list):
            link = continuation(place)
            return {"omitted": True, "total": len(item), "continue": link}, [link]
        if isinstance(item, dict):
            fields, names, links = [], [], []
            processed = 0
            for name, child in islice(item.items(), start, start + min(field_limit, 24)):
                child_pointer = place + "/" + str(name).replace("~", "~0").replace("/", "~1")
                small = len(review_runtime.canonical(child)) <= 1024
                candidate_fields = fields + [{name: child}] if small else fields
                candidate_links = links if small else links + [continuation(child_pointer)]
                if processed and len(review_runtime.canonical([candidate_fields, candidate_links])) > LIMIT // 3:
                    break
                fields, links = candidate_fields, candidate_links
                if not small:
                    names.append(name)
                processed += 1
            end = start + processed
            result = {"omitted": bool(links) or end < len(item), "fields": fields, "more_fields": end < len(item),
                      "continue_fields": names, "drilldowns": list(links),
                      "next_offset": end if end < len(item) else None}
            if end < len(item):
                result["continue"] = continuation(place, end)
                links.append(result["continue"])
            return result, links
        link = continuation(place)
        return {"omitted": True, "type": type(item).__name__, "bytes": size, "continue": link}, [link]

    if isinstance(value, list):
        items = value[offset:offset + page_size]
        while len(review_runtime.canonical(items)) > LIMIT // 2 and len(items) > 1:
            items = items[:max(1, len(items) // 2)]
        bounded, drilldowns = [], []
        for index, item in enumerate(items, offset):
            projected, links = preview(item, pointer + "/" + str(index))
            bounded.append(projected)
            drilldowns.extend(links)
        end = offset + len(items)
        result = {"artifact": artifact, "pointer": pointer, "scope": scope, "items": bounded,
                  "total": len(value), "next_offset": end if end < len(value) else None,
                  "view_complete": end >= len(value) and not drilldowns}
        if drilldowns:
            result["drilldowns"] = drilldowns
        if end < len(value):
            result["continue"] = continuation(pointer, end)
    elif isinstance(value, str):
        text = value[string_offset:string_offset + 2048]
        end = string_offset + len(text)
        result = {"artifact": artifact, "pointer": pointer, "scope": scope, "text": text,
                  "next_string_offset": end if end < len(value) else None, "view_complete": end >= len(value)}
        if end < len(value):
            result["continue"] = continuation(pointer, string_offset=end)
    else:
        projected, drilldowns = preview(value, pointer, offset, page_size)
        result = {"artifact": artifact, "pointer": pointer, "scope": scope, "value": projected,
                  "fields": [str(name)[:160] for name in islice(value, 40)] if isinstance(value, dict) else None,
                  "fields_total": len(value) if isinstance(value, dict) else None,
                  "view_complete": not drilldowns}
        if drilldowns:
            result["drilldowns"] = drilldowns
    if len(review_runtime.canonical(result)) >= LIMIT:
        raise ValueError("view page exceeds the response limit; use a smaller page or a more specific pointer")
    return result
