"""Build a bounded first evidence batch from the already captured branch patch."""
from __future__ import annotations

import re


CONTEXT_LINES = 20
REGULAR_MODES = {"100644", "100755"}
HUNK = re.compile(rb"@@ -(\d{1,10})(?:,(\d{1,10}))? \+(\d{1,10})(?:,(\d{1,10}))? @@(?: .*)?")
INDEX = re.compile(rb"index ([a-f0-9]{4,64})\.\.([a-f0-9]{4,64})(?: [0-7]{6})?")


def patch_sections(lines, headers=None):
    sections = {}
    section = None
    for raw in lines:
        line = raw.removesuffix(b"\n")
        if line.startswith(b"diff --git "):
            section = None
            if headers is None or line in headers:
                section = []
                sections.setdefault(line, []).append(section)
        elif section is not None:
            section.append(line)
    return sections


def intervals(section, path, change):
    old_blob, new_blob = change["old_blob"], change["new_blob"]
    if not isinstance(old_blob, str) or not isinstance(new_blob, str):
        return None
    if len(section) < 4 or section[1:3] != [b"--- a/" + path, b"+++ b/" + path]:
        return None
    index = INDEX.fullmatch(section[0])
    if not index or not all(blob.startswith(prefix.decode("ascii"))
                            for blob, prefix in zip((old_blob, new_blob), index.groups())):
        return None
    starts = [offset for offset, line in enumerate(section[3:], 3) if line.startswith(b"@@")]
    if not starts or starts[0] != 3:
        return None
    old_ranges, new_ranges = [], []
    additions = deletions = 0
    for start, end in zip(starts, starts[1:] + [len(section)]):
        match = HUNK.fullmatch(section[start])
        if match is None:
            return None
        old, old_count, new, new_count = (int(value or b"1") for value in match.groups())
        if min(old, old_count, new, new_count) < 1:
            return None
        body = [line[:1] for line in section[start + 1:end]
                if line != b"\\ No newline at end of file"]
        if set(body) - {b"-", b"+", b" "}:
            return None
        added, removed, context = body.count(b"+"), body.count(b"-"), body.count(b" ")
        if (removed + context, added + context) != (old_count, new_count):
            return None
        if ((old_ranges and old <= old_ranges[-1][1]) or
                (new_ranges and new <= new_ranges[-1][1])):
            return None
        old_ranges.append((old, old + old_count - 1))
        new_ranges.append((new, new + new_count - 1))
        additions += added
        deletions += removed
    if (additions, deletions) != (change.get("additions"), change.get("deletions")):
        return None
    return merge_ranges(old_ranges), merge_ranges(new_ranges)


def merge_ranges(ranges):
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 2 * CONTEXT_LINES + 1:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def initial_read_requests(context, manifest, full_requests):
    """Keep full reads whenever a source cannot use the captured coordinates."""
    import review_workspace as workspace

    repo = workspace.repository(manifest, manifest["selected_repo"])
    sources = repo["sources"]
    base = sources.get("base", {})
    head = sources.get("head", {})
    left = base if base.get("ref") == repo.get("diff_base") else sources.get("merge-base", {})
    if not left or left.get("ref") != repo.get("diff_base") or not base or not head:
        return None
    patch = f"repos/{repo['id']}/native/diff.patch"
    if patch not in repo.get("patches", []):
        return None

    parent_paths = {path for view in manifest.get("required_merge_views", []) for path in view.get("paths", [])}
    preview_paths = {path for row in repo["changed_files"] if row["source"] == "merge-preview"
                     for path in (row["path"], row.get("old_path"))}
    needed = {row["path"] for row in manifest["required_coverage"]
              if row["repo"] == repo["id"] and row["source"] == "head"}
    eligible_paths = needed - parent_paths - preview_paths
    changes = [row for row in repo["changed_files"]
               if row["source"] == "head" and row["path"] in eligible_paths
               and row.get("status") == "M" and row.get("old_path") is None and row.get("binary") is False
               and row.get("old_mode") == row.get("new_mode") in REGULAR_MODES
               and row.get("old_blob") and row.get("new_blob")
               and re.fullmatch(r"[A-Za-z0-9._/-]+", row["path"])]
    if not changes:
        return None
    headers = {change["path"]: patch_header(change["path"]) for change in changes}
    try:
        with workspace.safe_path(context, patch).open("rb") as stream:
            sections = patch_sections(stream, set(headers.values()))
    except OSError:
        return None
    reads = {}
    source_index = workspace.FixedSourceIndex()
    versions = {name: source for name, source in sources.items() if name in ("base", "head", "merge-preview")}
    blob_sources = {**versions, "diff-base": left}
    for change in changes:
        path = change["path"]
        encoded = path.encode("ascii")
        matching = sections.get(headers[path], [])
        if len(matching) != 1:
            continue
        if not all(matches_blob(source_index, source, path, change, name) for name, source in blob_sources.items()):
            continue
        mapped = intervals(matching[0], encoded, change)
        if mapped is not None:
            old, new = mapped
            reads[path] = {name: old if name == "base" else new for name in versions}

    if not reads:
        return None
    planned = []
    for request in full_requests:
        bounds = (reads.get(request.get("path"), {}).get(request.get("source"))
                  if request["op"] == "read" else None)
        if bounds is None:
            planned.append(request)
        else:
            planned.extend({**request, "id": f"hunk-{request['id']}-{index}",
                            "hunk": {"line_start": start, "line_end": end}}
                           for index, (start, end) in enumerate(bounds, 1))
    return planned


def patch_header(path):
    encoded = path.encode("ascii")
    return b"diff --git a/" + encoded + b" b/" + encoded


def matches_blob(index, source, path, change, name):
    side = "old" if name in ("base", "diff-base") else "new"
    entries = index.entries(source, path)
    return len(entries) == 1 and entries[0].get("kind") == "blob" and (
        entries[0].get("blob_sha"), entries[0].get("mode")) == (
        change[side + "_blob"], change[side + "_mode"])
