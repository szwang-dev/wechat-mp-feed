"""Archive article assets for high-value retained articles."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .storage import Store


def cache_full_archive_assets(
    *,
    store: Store,
    output_dir: Path,
    limit: int = 100,
    timeout: float = 30.0,
    overwrite: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = store.list_assets_for_archive(limit=limit, cached=overwrite)
    results: list[dict[str, Any]] = []
    ok = 0
    failed = 0

    for asset in assets:
        result = cache_asset(asset, output_dir=output_dir, timeout=timeout, overwrite=overwrite)
        results.append(result)
        if result["ok"]:
            ok += 1
            store.update_article_asset_cache(
                asset_id=asset["id"],
                local_path=result["local_path"],
                download_status="cached",
            )
        else:
            failed += 1
            store.update_article_asset_cache(
                asset_id=asset["id"],
                local_path=asset.get("local_path"),
                download_status="failed",
            )
        store.refresh_article_archive_status(asset["article_id"])

    return {
        "ok": True,
        "assets_seen": len(assets),
        "assets_cached": ok,
        "assets_failed": failed,
        "output_dir": str(output_dir),
        "items": results[:20],
        "items_truncated": max(0, len(results) - 20),
    }


def cache_asset(asset: dict[str, Any], *, output_dir: Path, timeout: float, overwrite: bool) -> dict[str, Any]:
    url = normalize_asset_url(asset["url"])
    target = output_dir / asset["article_id"] / asset_filename(asset, url)
    if target.exists() and not overwrite:
        return {"ok": True, "asset_id": asset["id"], "url": url, "local_path": str(target), "status": "already_cached"}
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 mpfeed-asset-cache"})
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
    except Exception as exc:  # pragma: no cover - exact urllib errors vary by runtime
        return {"ok": False, "asset_id": asset["id"], "url": url, "error": str(exc)}

    if not target.suffix:
        suffix = mimetypes.guess_extension(content_type) or ".bin"
        target = target.with_suffix(suffix)
    target.write_bytes(body)
    return {
        "ok": True,
        "asset_id": asset["id"],
        "url": url,
        "local_path": str(target),
        "bytes": len(body),
        "content_type": content_type,
        "status": "cached",
    }


def normalize_asset_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


def asset_filename(asset: dict[str, Any], url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix
    if suffix and len(suffix) <= 8:
        return f"{asset['id']}{suffix}"
    return asset["id"]
