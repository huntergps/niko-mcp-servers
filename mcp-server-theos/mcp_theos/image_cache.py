"""Filesystem-backed image cache for product photos.

The Theos visor app (Flutter, see /Users/elmers/Documents/develop/2026/
visor) uses a two-step fetch pattern that we mirror here:

1. ``visor_datos(codbar, dar_imagen=0)`` — light call returning the
   product header plus the image version tag ``fecha_mod_imagen``.
2. If our cached version matches the server's version → serve from
   disk (no second round-trip, no 440KB base64 transferred).
3. Otherwise → second call with ``dar_imagen=1`` to refetch the image
   and update the cache.

Storage layout::

    <cache_root>/
        <tenant_id>/
            <code>.png      — raw image bytes
            <code>.meta     — server ``fecha_mod_imagen`` string

Eviction policy: TTL (default 24h) + LRU (default 1000 files). The
cleanup pass runs opportunistically every Nth save to avoid blocking
the request path.

Concurrency: an in-flight Future dedup map prevents two concurrent
requests for the same ``(tenant, code)`` from both hitting Velneo;
the second one awaits the first's result.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_filename(code: str) -> str:
    """Sanitize a product code so it can be used as a filename."""
    return _SAFE_NAME.sub("_", code.strip()) or "_"


class ImageCache:
    """Persistent on-disk image cache for ``get_product_image``.

    Single instance per process; the runtime module-level instance is
    exposed via :func:`get_cache`. Reads / writes are filesystem ops —
    safe to call from async code as long as the working set fits in
    OS page cache.
    """

    def __init__(
        self,
        root_dir: str | os.PathLike[str],
        *,
        ttl_seconds: int = 24 * 3600,
        max_files: int = 1000,
        cleanup_every_n_saves: int = 50,
    ) -> None:
        self.root = Path(root_dir)
        self.ttl = ttl_seconds
        self.max_files = max_files
        self._cleanup_every = cleanup_every_n_saves
        self._save_count = 0
        # Per-key locks to dedup concurrent fetches.
        self._inflight_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[bytes | None]] = {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("image_cache: could not create root %s: %s", self.root, exc)

    # ---- paths ----------------------------------------------------------

    def _tenant_dir(self, tenant_id: str) -> Path:
        return self.root / _safe_filename(tenant_id)

    def _path(self, tenant_id: str, code: str) -> Path:
        return self._tenant_dir(tenant_id) / f"{_safe_filename(code)}.png"

    def _meta_path(self, tenant_id: str, code: str) -> Path:
        return self._tenant_dir(tenant_id) / f"{_safe_filename(code)}.meta"

    # ---- read -----------------------------------------------------------

    def get_cached(
        self, tenant_id: str, code: str,
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Return (bytes, server_version) if cached & not expired.

        ``server_version`` is the ``fecha_mod_imagen`` string that was
        present on the row when the cache was last written, or
        ``None`` if no meta was saved (legacy entries / version-less
        servers).
        """
        path = self._path(tenant_id, code)
        if not path.exists():
            return None, None
        try:
            stat = path.stat()
        except OSError:
            return None, None
        age = time.time() - stat.st_mtime
        if age > self.ttl:
            # Expired — drop it.
            self._unlink(path)
            return None, None
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("image_cache: read failed %s: %s", path, exc)
            return None, None
        meta_path = self._meta_path(tenant_id, code)
        version: Optional[str] = None
        if meta_path.exists():
            try:
                version = meta_path.read_text(encoding="utf-8").strip() or None
            except OSError:
                version = None
        # Touch atime+mtime so this becomes "recently used" for LRU.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data, version

    # ---- write ----------------------------------------------------------

    def save(
        self,
        tenant_id: str,
        code: str,
        bytes_data: bytes,
        version: Optional[str] = None,
    ) -> None:
        """Persist bytes + optional version tag. Best-effort.

        Triggers a cleanup pass every ``cleanup_every_n_saves`` writes
        to keep the cache within size bounds without paying the cost
        on every request.
        """
        if not bytes_data:
            return
        path = self._path(tenant_id, code)
        meta_path = self._meta_path(tenant_id, code)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write via temp + rename so a partial write
            # never leaves a corrupted .png on disk.
            tmp = path.with_suffix(".png.tmp")
            tmp.write_bytes(bytes_data)
            tmp.replace(path)
            if version:
                meta_tmp = meta_path.with_suffix(".meta.tmp")
                meta_tmp.write_text(version, encoding="utf-8")
                meta_tmp.replace(meta_path)
            elif meta_path.exists():
                # Server stopped sending a version — drop the stale meta.
                self._unlink(meta_path)
        except OSError as exc:
            logger.warning("image_cache: save failed %s: %s", path, exc)
            return
        self._save_count += 1
        if self._save_count % self._cleanup_every == 0:
            try:
                self.cleanup()
            except Exception as exc:  # noqa: BLE001
                logger.warning("image_cache: cleanup failed: %s", exc)

    # ---- maintenance ----------------------------------------------------

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def cleanup(self) -> dict[str, int]:
        """Drop expired files + enforce ``max_files`` via LRU.

        Returns a small dict with counters useful in /health.
        """
        expired = 0
        survivors: list[tuple[Path, float]] = []
        now = time.time()
        if not self.root.exists():
            return {"expired": 0, "evicted": 0, "kept": 0}
        for png in self.root.glob("*/*.png"):
            try:
                stat = png.stat()
            except OSError:
                continue
            age = now - stat.st_mtime
            if age > self.ttl:
                self._unlink(png)
                self._unlink(png.with_suffix(".meta"))
                expired += 1
            else:
                survivors.append((png, stat.st_mtime))
        evicted = 0
        if len(survivors) > self.max_files:
            survivors.sort(key=lambda t: t[1])  # oldest first
            overflow = len(survivors) - self.max_files
            for png, _ in survivors[:overflow]:
                self._unlink(png)
                self._unlink(png.with_suffix(".meta"))
                evicted += 1
        return {
            "expired": expired,
            "evicted": evicted,
            "kept": max(0, len(survivors) - evicted),
        }

    def stats(self) -> dict[str, int | float]:
        """Cheap stats — files + total size — for /health debugging."""
        if not self.root.exists():
            return {"files": 0, "size_bytes": 0, "size_mb": 0.0}
        files = 0
        size = 0
        for png in self.root.glob("*/*.png"):
            try:
                size += png.stat().st_size
                files += 1
            except OSError:
                continue
        return {
            "files": files,
            "size_bytes": size,
            "size_mb": round(size / (1024 * 1024), 2),
        }

    # ---- in-flight dedup -----------------------------------------------

    async def begin_fetch(self, tenant_id: str, code: str) -> tuple[asyncio.Future[bytes | None], bool]:
        """Reserve a slot to fetch the image bytes for (tenant, code).

        Returns ``(future, is_leader)``. The leader (first caller for a
        given key) does the network fetch and resolves the future via
        :meth:`finish_fetch`. Followers await the same future and get
        the leader's result for free — no second round-trip to Velneo.
        """
        key = f"{tenant_id}:{_safe_filename(code)}"
        async with self._inflight_lock:
            fut = self._inflight.get(key)
            if fut is not None:
                return fut, False
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._inflight[key] = fut
            return fut, True

    async def finish_fetch(
        self, tenant_id: str, code: str,
        fut: asyncio.Future[bytes | None],
        value: bytes | None,
    ) -> None:
        key = f"{tenant_id}:{_safe_filename(code)}"
        if not fut.done():
            fut.set_result(value)
        async with self._inflight_lock:
            self._inflight.pop(key, None)


# Module-level singleton, initialized lazily so test harnesses can
# override the cache directory via the settings before the first call.

_INSTANCE: Optional[ImageCache] = None


def get_cache() -> ImageCache:
    """Return the process-wide :class:`ImageCache` singleton."""
    global _INSTANCE
    if _INSTANCE is None:
        from mcp_theos.config import settings
        root = (
            getattr(settings, "image_cache_dir", "")
            or os.environ.get("IMAGE_CACHE_DIR")
            or "/var/cache/mcp-theos/images"
        )
        ttl = int(
            getattr(settings, "image_cache_ttl_seconds", 0)
            or os.environ.get("IMAGE_CACHE_TTL", 24 * 3600)
        )
        max_files = int(
            getattr(settings, "image_cache_max_files", 0)
            or os.environ.get("IMAGE_CACHE_MAX_FILES", 1000)
        )
        _INSTANCE = ImageCache(
            root, ttl_seconds=ttl, max_files=max_files,
        )
    return _INSTANCE
