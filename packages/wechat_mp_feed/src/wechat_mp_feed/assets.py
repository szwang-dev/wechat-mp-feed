"""Archive article assets for high-value retained articles."""

from __future__ import annotations

import io
import mimetypes
import re
import struct
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from .storage import Store

SKIP_KEYWORDS = (
    "扫码",
    "二维码",
    "关注公众号",
    "关注我们",
    "长按识别",
    "加入群",
    "交流群",
    "课程",
    "报名",
    "直播",
    "免责声明",
    "风险提示",
    "广告",
    "商务合作",
    "点击蓝字",
    "点击下载",
    "下载客户端",
    "APP下载",
    "app下载",
    "执业证书编号",
    "分析师承诺",
)

KEEP_KEYWORDS = (
    "收益率",
    "估值",
    "指数",
    "行业",
    "配置",
    "资金流",
    "因子",
    "回撤",
    "胜率",
    "相关性",
    "库存",
    "价格",
    "同比",
    "环比",
    "净流入",
    "净流出",
    "图表",
    "表格",
)


def cache_full_archive_assets(
    *,
    store: Store,
    output_dir: Path,
    limit: int = 100,
    timeout: float = 30.0,
    overwrite: bool = False,
    filter_assets: bool = True,
    ocr: str = "off",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = store.list_assets_for_archive(limit=limit, cached=overwrite)
    results: list[dict[str, Any]] = []
    ok = 0
    failed = 0
    skipped = 0
    ocr_engine = make_asset_ocr_engine(ocr) if filter_assets else None

    for asset in assets:
        result = cache_asset(
            asset,
            output_dir=output_dir,
            timeout=timeout,
            overwrite=overwrite,
            filter_assets=filter_assets,
            ocr_engine=ocr_engine,
        )
        results.append(result)
        if result["ok"]:
            ok += 1
            store.update_article_asset_cache(
                asset_id=asset["id"],
                local_path=result["local_path"],
                download_status="cached",
                metadata_patch=result.get("metadata"),
            )
        elif result.get("status") == "skipped":
            skipped += 1
            store.update_article_asset_cache(
                asset_id=asset["id"],
                local_path=None,
                download_status="skipped",
                metadata_patch=result.get("metadata"),
            )
        else:
            failed += 1
            store.update_article_asset_cache(
                asset_id=asset["id"],
                local_path=asset.get("local_path"),
                download_status="failed",
                metadata_patch=result.get("metadata"),
            )
        store.refresh_article_archive_status(asset["article_id"])

    return {
        "ok": True,
        "assets_seen": len(assets),
        "assets_cached": ok,
        "assets_failed": failed,
        "assets_skipped": skipped,
        "output_dir": str(output_dir),
        "items": results[:20],
        "items_truncated": max(0, len(results) - 20),
    }


def cache_asset(
    asset: dict[str, Any],
    *,
    output_dir: Path,
    timeout: float,
    overwrite: bool,
    filter_assets: bool = True,
    ocr_engine: Any = None,
) -> dict[str, Any]:
    url = normalize_asset_url(asset["url"])
    target = output_dir / asset["article_id"] / asset_filename(asset, url)
    if target.exists() and not overwrite:
        return {"ok": True, "asset_id": asset["id"], "url": url, "local_path": str(target), "status": "already_cached"}
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 mpfeed-asset-cache"})
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
    except Exception as exc:  # pragma: no cover - exact urllib errors vary by runtime
        return {"ok": False, "asset_id": asset["id"], "url": url, "error": str(exc)}

    metadata = build_asset_metadata(asset=asset, url=url, body=body, content_type=content_type, ocr_engine=ocr_engine)
    if filter_assets:
        decision = classify_asset_for_archive(asset=asset, metadata=metadata)
        metadata["archive_decision"] = decision
        if decision["action"] == "skip":
            return {
                "ok": False,
                "asset_id": asset["id"],
                "url": url,
                "status": "skipped",
                "skip_reason": decision["reason"],
                "metadata": metadata,
            }

    target.parent.mkdir(parents=True, exist_ok=True)
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
        "metadata": metadata,
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


def build_asset_metadata(
    *,
    asset: dict[str, Any],
    url: str,
    body: bytes,
    content_type: str,
    ocr_engine: Any = None,
) -> dict[str, Any]:
    existing = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    metadata: dict[str, Any] = dict(existing or {})
    metadata["content_type"] = content_type
    metadata["bytes"] = len(body)
    width, height, fmt = image_size(body)
    if width is not None:
        metadata["width"] = width
    if height is not None:
        metadata["height"] = height
    if fmt:
        metadata["image_format"] = fmt
    metadata["url_features"] = url_features(url)

    qr_result = detect_qr(body)
    if qr_result:
        metadata["qr_detected"] = True
        if qr_result is not True:
            metadata["qr_payload"] = qr_result

    visual_stats = analyze_visual_stats(body)
    if visual_stats:
        metadata["visual_stats"] = visual_stats

    if ocr_engine and should_run_asset_ocr(metadata):
        ocr_text = run_asset_ocr(body, ocr_engine)
        if ocr_text:
            metadata["ocr_text"] = ocr_text[:2000]
    return metadata


def classify_asset_for_archive(*, asset: dict[str, Any], metadata: dict[str, Any]) -> dict[str, str]:
    content_type = str(metadata.get("content_type") or "")
    width = as_int(metadata.get("width"))
    height = as_int(metadata.get("height"))
    byte_count = as_int(metadata.get("bytes")) or 0
    block_index = as_int(asset.get("block_index"))
    text = str(metadata.get("ocr_text") or "")

    if metadata.get("qr_detected"):
        return {"action": "skip", "reason": "qr_code"}
    if "svg" in content_type:
        return {"action": "skip", "reason": "svg_or_vector"}
    if byte_count and byte_count < 2048:
        return {"action": "skip", "reason": "tiny_file"}
    if width and height:
        area = width * height
        short_side = min(width, height)
        long_side = max(width, height)
        if area < 12000 or short_side < 80:
            return {"action": "skip", "reason": "small_icon_or_logo"}
        if short_side and long_side / short_side >= 8 and short_side <= 120:
            return {"action": "skip", "reason": "divider_or_banner"}

    if text:
        if looks_like_author_profile_text(text):
            return {"action": "skip", "reason": "ocr_author_profile"}
        if contains_any(text, SKIP_KEYWORDS):
            return {"action": "skip", "reason": "ocr_low_value_or_promotion"}
        if contains_any(text, KEEP_KEYWORDS):
            return {"action": "keep", "reason": "ocr_content_signal"}

    visual_stats = metadata.get("visual_stats") if isinstance(metadata.get("visual_stats"), dict) else {}
    if looks_like_low_detail_decorative_image(visual_stats):
        return {"action": "skip", "reason": "low_detail_decorative_image"}

    if block_index is not None and block_index >= 80 and width and height and width * height < 100000:
        return {"action": "skip", "reason": "late_small_template_image"}
    return {"action": "keep", "reason": "default_keep"}


def url_features(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    fmt = query.get("wx_fmt", [""])[0]
    return {
        "host": parsed.netloc,
        "path_suffix": Path(parsed.path).suffix,
        "wx_fmt": fmt,
    }


def image_size(body: bytes) -> tuple[int | None, int | None, str | None]:
    if body.startswith(b"\x89PNG\r\n\x1a\n") and len(body) >= 24:
        width, height = struct.unpack(">II", body[16:24])
        return int(width), int(height), "png"
    if body[:6] in (b"GIF87a", b"GIF89a") and len(body) >= 10:
        width, height = struct.unpack("<HH", body[6:10])
        return int(width), int(height), "gif"
    if body.startswith(b"\xff\xd8"):
        return jpeg_size(body)
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return webp_size(body)
    return None, None, None


def jpeg_size(body: bytes) -> tuple[int | None, int | None, str | None]:
    stream = io.BytesIO(body)
    stream.read(2)
    while True:
        marker_start = stream.read(1)
        if not marker_start:
            return None, None, "jpeg"
        if marker_start != b"\xff":
            continue
        marker = stream.read(1)
        while marker == b"\xff":
            marker = stream.read(1)
        if marker in {b"\xd8", b"\xd9"}:
            continue
        size_bytes = stream.read(2)
        if len(size_bytes) != 2:
            return None, None, "jpeg"
        size = struct.unpack(">H", size_bytes)[0]
        if size < 2:
            return None, None, "jpeg"
        if marker and marker[0] in range(0xC0, 0xCF) and marker not in {b"\xc4", b"\xc8", b"\xcc"}:
            data = stream.read(size - 2)
            if len(data) >= 5:
                height, width = struct.unpack(">HH", data[1:5])
                return int(width), int(height), "jpeg"
            return None, None, "jpeg"
        stream.seek(size - 2, io.SEEK_CUR)


def webp_size(body: bytes) -> tuple[int | None, int | None, str | None]:
    if len(body) < 30:
        return None, None, "webp"
    kind = body[12:16]
    if kind == b"VP8X" and len(body) >= 30:
        width = 1 + int.from_bytes(body[24:27], "little")
        height = 1 + int.from_bytes(body[27:30], "little")
        return width, height, "webp"
    if kind == b"VP8 " and len(body) >= 30:
        width = struct.unpack("<H", body[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", body[28:30])[0] & 0x3FFF
        return int(width), int(height), "webp"
    if kind == b"VP8L" and len(body) >= 25:
        b0, b1, b2, b3 = body[21], body[22], body[23], body[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return int(width), int(height), "webp"
    return None, None, "webp"


def detect_qr(body: bytes) -> bool | str:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return False
    try:
        image = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return False
        detector = cv2.QRCodeDetector()
        data, points, _ = detector.detectAndDecode(image)
        if points is not None:
            return data or True
    except Exception:
        return False
    return False


def analyze_visual_stats(body: bytes) -> dict[str, Any]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return {}
    try:
        image = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            try:
                from PIL import Image  # type: ignore

                pil_image = Image.open(io.BytesIO(body))
                pil_image.seek(0)
                image = cv2.cvtColor(np.array(pil_image.convert("RGB")), cv2.COLOR_RGB2BGR)
            except Exception:
                return {}
        resized = cv2.resize(image, (64, 64), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        quantized = (resized // 32).reshape(-1, 3)
        unique_colors = len({tuple(pixel) for pixel in quantized})
        non_white_ratio = float((np.min(resized, axis=2) < 245).mean())
        edge_density = float((edges > 0).mean())
    except Exception:
        return {}
    return {
        "edge_density": round(edge_density, 6),
        "non_white_ratio": round(non_white_ratio, 6),
        "quantized_unique_colors": unique_colors,
    }


def looks_like_low_detail_decorative_image(visual_stats: dict[str, Any]) -> bool:
    unique_colors = as_int(visual_stats.get("quantized_unique_colors")) or 0
    edge_density = as_float(visual_stats.get("edge_density"))
    non_white_ratio = as_float(visual_stats.get("non_white_ratio"))
    if edge_density is None or non_white_ratio is None:
        return False
    if unique_colors <= 3 and edge_density < 0.02:
        return True
    return unique_colors <= 8 and edge_density < 0.12 and 0.08 <= non_white_ratio <= 0.45


def make_asset_ocr_engine(ocr: str) -> Any:
    if ocr == "off":
        return None
    if ocr != "paddle":
        raise ValueError("asset OCR supports only 'off' or 'paddle'")
    from .media_import import make_paddle_ocr

    return make_paddle_ocr(lang="ch")


def should_run_asset_ocr(metadata: dict[str, Any]) -> bool:
    width = as_int(metadata.get("width")) or 0
    height = as_int(metadata.get("height")) or 0
    byte_count = as_int(metadata.get("bytes")) or 0
    if metadata.get("qr_detected"):
        return False
    if width and height and (width * height < 40000 or min(width, height) < 120):
        return False
    return byte_count >= 4096


def run_asset_ocr(body: bytes, ocr_engine: Any) -> str:
    suffix = ".png"
    _, _, fmt = image_size(body)
    if fmt:
        suffix = f".{fmt}"
    with tempfile.NamedTemporaryFile(suffix=suffix) as temp:
        temp.write(body)
        temp.flush()
        try:
            result = ocr_engine.ocr(temp.name, cls=True)
        except TypeError as exc:
            if "cls" not in str(exc):
                raise
            result = ocr_engine.ocr(temp.name)
    texts: list[str] = []
    for line in flatten_ocr_result(result):
        if isinstance(line, str):
            texts.append(line)
    return "\n".join(texts)


def flatten_ocr_result(result: Any) -> list[str]:
    texts: list[str] = []
    if not result:
        return texts
    for page in result:
        if not page:
            continue
        if isinstance(page, dict):
            rec_texts = page.get("rec_texts")
            if isinstance(rec_texts, list):
                texts.extend(text for text in rec_texts if isinstance(text, str))
            continue
        for item in page:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text_part = item[1]
                if isinstance(text_part, (list, tuple)) and text_part:
                    text = text_part[0]
                    if isinstance(text, str):
                        texts.append(text)
                elif isinstance(text_part, str):
                    texts.append(text_part)
    return texts


def as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = re.sub(r"\s+", "", unquote(text)).lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def looks_like_author_profile_text(text: str) -> bool:
    normalized = re.sub(r"\s+", "", unquote(text)).lower()
    if "分析师" not in normalized and "研究助理" not in normalized:
        return False
    profile_signals = (
        "首席分析师",
        "高级分析师",
        "研究助理",
        "加入",
        "从业",
        "经济学学士",
        "经济学硕士",
        "博士",
        "覆盖",
        "执业证书编号",
        "sac",
        "sfc",
        "证券投资咨询",
    )
    return sum(1 for signal in profile_signals if signal in normalized) >= 2
