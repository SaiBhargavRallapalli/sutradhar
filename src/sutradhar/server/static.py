"""Preloaded, compressed static assets with validators and safe path handling."""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from pathlib import Path

_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}


@dataclass(frozen=True, slots=True)
class StaticResponse:
    status: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class _Asset:
    body: bytes
    compressed: bytes
    content_type: str
    etag: str
    cache_control: str


class StaticStore:
    """Read allowed web assets once, then serve them without per-request I/O."""

    def __init__(self, root: Path | None) -> None:
        self._assets: dict[str, _Asset] = {}
        if root is None:
            return
        resolved = root.resolve()
        if not (resolved / "index.html").is_file():
            return
        for file_path in resolved.rglob("*"):
            if not file_path.is_file() or file_path.suffix not in _CTYPES:
                continue
            relative = "/" + file_path.relative_to(resolved).as_posix()
            body = file_path.read_bytes()
            # Weak because gzip and identity encodings are semantically the
            # same representation but have different transfer bytes.
            etag = 'W/"' + hashlib.sha256(body).hexdigest()[:24] + '"'
            cache = "no-cache" if file_path.suffix == ".html" else "public, max-age=3600"
            self._assets[relative] = _Asset(
                body=body,
                compressed=gzip.compress(body, compresslevel=9),
                content_type=_CTYPES[file_path.suffix],
                etag=etag,
                cache_control=cache,
            )

    def get(
        self,
        raw_path: str,
        *,
        accept_encoding: str = "",
        if_none_match: str = "",
        head_only: bool = False,
    ) -> StaticResponse:
        path = "/index.html" if raw_path in {"", "/"} else raw_path
        if ".." in path.split("/"):
            body = b"403 Forbidden"
            return StaticResponse(
                "403 Forbidden",
                (("Content-Type", "text/plain; charset=utf-8"),),
                body,
            )
        asset = self._assets.get(path)
        if asset is None:
            body = b"404 Not Found"
            return StaticResponse(
                "404 Not Found",
                (("Content-Type", "text/plain; charset=utf-8"),),
                body,
            )

        use_gzip = "gzip" in accept_encoding.lower() and len(asset.body) >= 512
        base_headers: tuple[tuple[str, str], ...] = (
            ("Content-Type", asset.content_type),
            ("Cache-Control", asset.cache_control),
            ("ETag", asset.etag),
            ("Vary", "Accept-Encoding"),
        )
        if use_gzip:
            base_headers = (*base_headers, ("Content-Encoding", "gzip"))
        if if_none_match == asset.etag:
            return StaticResponse("304 Not Modified", (*base_headers, ("Content-Length", "0")), b"")

        body = asset.compressed if use_gzip else asset.body
        headers: tuple[tuple[str, str], ...] = (
            *base_headers,
            ("Content-Length", str(len(body))),
        )
        return StaticResponse("200 OK", headers, b"" if head_only else body)

    @property
    def available(self) -> bool:
        return "/index.html" in self._assets

    @property
    def asset_count(self) -> int:
        return len(self._assets)


__all__ = ["StaticResponse", "StaticStore"]
