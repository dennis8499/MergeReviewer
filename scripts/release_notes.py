#!/usr/bin/env python3
"""Attach versioned repository notes to an already verified formal release."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import urllib.parse

import release_ci as ci


def public_notes(text, repository, tag):
    """Keep repository-relative documentation links usable on the Release page."""
    def replace(match):
        target = match.group(1)
        if target.startswith(("https://", "http://", "#")):
            return match.group(0)
        path, separator, fragment = target.partition("#")
        resolved = posixpath.normpath(posixpath.join("docs/releases", path))
        ci.require(not resolved.startswith("../") and not resolved.startswith("/"), "Release note link escapes repository")
        base = f"https://github.com/{repository}/blob/{urllib.parse.quote(tag, safe='')}/"
        return "](" + base + urllib.parse.quote(resolved, safe="/") + ("#" + fragment if separator else "") + ")"
    return re.sub(r"\]\(([^)]+)\)", replace, text.strip())


def attach(api, source, tag, publication, notes):
    ci.require(isinstance(publication, dict) and publication.get("tag") == tag and publication.get("sha") == source["sha"],
               "Publication receipt does not identify the checked-out tag")
    ci.require(isinstance(notes, str) and notes.strip(), "Versioned release notes are empty")
    ci.validate_tag_target(api, tag, source)
    release = api.request("releases/tags/" + urllib.parse.quote(tag, safe=""))
    ci.require(isinstance(release, dict) and release.get("tag_name") == tag and release.get("draft") is False,
               "Verified formal release is unavailable")
    ci.require(type(release.get("id")) is int and release["id"] > 0, "Invalid formal release identity")
    body = release.get("body") or ""
    ci.require(isinstance(body, str), "Release body must be text")
    start, end = f"<!-- release-notes:{tag}:start -->", f"<!-- release-notes:{tag}:end -->"
    block = start + "\n" + public_notes(notes, source["repository"], tag) + "\n" + end
    if start in body or end in body:
        ci.require(body.count(start) == 1 and body.count(end) == 1 and block in body,
                   "Existing versioned release notes differ; refusing to overwrite")
        return {"tag": tag, "updated": False}
    updated = block + ("\n\n" + body if body else "")
    api.request(f"releases/{release['id']}", "PATCH", {"body": updated})
    actual = api.request("releases/tags/" + urllib.parse.quote(tag, safe=""))
    ci.require(isinstance(actual, dict) and actual.get("id") == release["id"] and
               actual.get("draft") is False and actual.get("body") == updated,
               "Published release notes could not be verified")
    return {"tag": tag, "updated": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        source = ci.identity(args.root, repository)
        tag = os.environ.get("GITHUB_REF_NAME", "")
        ci.require(tag == "v" + source["version"], "Tag/version mismatch")
        notes_path = args.root / "docs/releases" / (tag + ".md")
        if not notes_path.is_file():
            print(json.dumps({"tag": tag, "updated": False, "reason": "no versioned release notes"}))
            return 0
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        ci.require(token, "GitHub token missing")
        publication = json.loads(args.publication.read_text(encoding="utf-8"))
        result = attach(ci.GitHub(repository, token), source, tag, publication,
                        notes_path.read_text(encoding="utf-8"))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ci.ProofError, KeyError, ValueError, OSError, json.JSONDecodeError) as error:
        print("release-notes: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
