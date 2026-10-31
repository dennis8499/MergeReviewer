"""Shared safe decoding and display redaction for captured source content."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

_BINARY_SUFFIXES = {
    ".dll", ".exe", ".so", ".dylib", ".pdb", ".class", ".jar", ".war", ".ear",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".pdf", ".zip",
    ".7z", ".gz", ".bz2", ".xz", ".rar", ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".sqlite", ".db", ".bin", ".dat",
}
_MAGIC = (b"MZ", b"\x7fELF", b"\x89PNG\r\n\x1a\n", b"PK\x03\x04", b"\xff\xd8\xff")
_SENSITIVE = re.compile(
    r"(?i)([\"']?\b(?:password|passwd|pwd|token|api[_-]?key|secret|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|credential|authorization|connection[_-]?string)\b[\"']?"
    r"\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^;,\s&]+)"
)
_SENSITIVE_LINE = re.compile(r"(?i)connection[_ -]?string|data\s*source|initial\s*catalog|server\s*=|host\s*=")
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_BEARER = re.compile(r"(?i)(\bBearer\s+)\S+")
_PRIVATE_KEY = re.compile(r"(?i)-----BEGIN [^-]*PRIVATE KEY-----")
_PRIVATE_END = re.compile(r"(?i)-----END [^-]*PRIVATE KEY-----")


def decode_source(raw: bytes, path: str = "") -> tuple[list[str] | None, str | None, bool]:
    """Return byte-line-aligned text, encoding label, and whether decoding lost data.

    UTF-8 and BOM-marked UTF-16 are preferred. Taiwan legacy text is tried as
    CP950. Unknown byte sequences get a visibly lossy UTF-8 replacement view;
    binary blobs are returned without fabricated text lines.
    """
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    if suffix in _BINARY_SUFFIXES or any(raw.startswith(magic) for magic in _MAGIC):
        return None, None, False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = None
        for candidate in ("utf-8", "cp950"):
            try:
                raw.decode(candidate, errors="strict")
            except UnicodeDecodeError:
                continue
            encoding = candidate
            break
    if encoding is None:
        return [line.decode("utf-8", errors="replace") for line in raw.splitlines()], "unknown", True
    if b"\0" in raw and encoding != "utf-16":
        return None, None, False
    try:
        decoded = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return [line.decode("utf-8", errors="replace") for line in raw.splitlines()], "unknown", True
    # Preserve Git's byte-based line numbering; Unicode separators inside text
    # must not invent evidence lines.
    byte_lines = raw.splitlines()
    if not raw:
        byte_lines = []
    if encoding == "utf-16":
        # Split only on physical CR/LF delimiters. str.splitlines() also treats
        # Unicode separators as lines, which would break Git byte line numbers.
        lines = re.split(r"\r\n|\r|\n", decoded)
        if lines and not lines[-1] and decoded.endswith(("\r", "\n")):
            lines.pop()
        if lines:
            lines[0] = lines[0].removeprefix("\ufeff")
    else:
        lines = [line.decode(encoding, errors="strict") for line in byte_lines]
        if encoding == "utf-8" and lines:
            lines[0] = lines[0].removeprefix("\ufeff")
    sample = raw[:8192]
    if encoding != "utf-16" and sample:
        controls = sum(byte < 9 or 13 < byte < 32 for byte in sample)
        if controls / len(sample) > 0.05:
            return None, None, False
    return lines, encoding, False


def redact_lines(lines: list[str], path: str = "") -> tuple[list[str], bool]:
    """Mask common secret values while preserving one output row per source row."""
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    if suffix in {".pem", ".key", ".pfx", ".p12"}:
        return (["[私鑰內容已省略]" for _ in lines], bool(lines))
    result, changed, in_private_key = [], False, False
    for line in lines:
        if in_private_key or _PRIVATE_KEY.search(line):
            in_private_key = True
            result.append("[私鑰內容已省略]")
            changed = True
            if _PRIVATE_END.search(line):
                in_private_key = False
            continue
        if _SENSITIVE_LINE.search(line):
            result.append("[含認證或連線設定的行已省略]")
            changed = True
            continue
        value = _SENSITIVE.sub(r"\1[已遮蔽]", line)
        value = _URL_CREDENTIALS.sub(r"\1[已遮蔽]@", value)
        value = _BEARER.sub(r"\1[已遮蔽]", value)
        result.append(value)
        changed |= value != line
    return result, changed
