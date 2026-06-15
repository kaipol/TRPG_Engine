"""Shared document extraction helpers backed by MinerU OCR."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET

import fastapi

from .campaign_storage import sanitize_asset_name, unique_path
from .local_config import get_campaign_import_settings


DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ""}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VISION_IMAGE_EXTENSIONS = IMAGE_EXTENSIONS | {".tif", ".tiff", ".ppm", ".pgm", ".pbm"}
MINERU_FLASH_MAX_BYTES = 10 * 1024 * 1024
ProgressCallback = Callable[[int, str, list[str]], None]


@dataclass
class MinerUExtractionResult:
    text: str = ""
    assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engine: str = "mineru"


@dataclass
class VisionFallbackImage:
    label: str
    data_url: str
    asset_name: str = ""
    width: int = 0
    height: int = 0


@dataclass
class VisionFallbackPayload:
    text: str = ""
    images: list[VisionFallbackImage] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _ImageCandidate:
    label: str
    filename: str
    data: bytes


def decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise fastapi.HTTPException(status_code=400, detail="文件编码无法识别，请转为 UTF-8 后重试")


def extract_text_document(raw: bytes, filename: str) -> tuple[str, list[str]]:
    """Extract text for non-campaign document uploads."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return decode_text_bytes(raw), []
    if suffix in DOCUMENT_EXTENSIONS:
        result = extract_document_with_mineru(raw, filename, assets_dir=None)
        return result.text, result.warnings
    raise fastapi.HTTPException(status_code=400, detail="仅支持 TXT、Markdown、PDF、DOCX、DOC 文件")


def extract_document_with_mineru(
    raw: bytes,
    filename: str,
    assets_dir: str | None = None,
    ocr_enabled: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> MinerUExtractionResult:
    """Run MinerU extraction, then return extracted Markdown text and copied images."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return MinerUExtractionResult(text=decode_text_bytes(raw), engine="text")
    if suffix not in DOCUMENT_EXTENSIONS:
        raise fastapi.HTTPException(status_code=400, detail="仅支持 PDF、DOCX、DOC、TXT、Markdown 文件")

    settings = get_campaign_import_settings()
    command = str(settings.get("mineru_command") or "mineru-open-api").strip() or "mineru-open-api"
    command_parts, command_error = _resolve_mineru_command(_command_parts(command))
    if not command_parts:
        warning = _mineru_not_found_warning(command, command_error)
        if progress_callback:
            progress_callback(36, warning, [warning])
        return MinerUExtractionResult(
            warnings=[
                warning
            ]
        )

    timeout_seconds = float(settings.get("mineru_timeout_seconds") or 300)
    language = str(settings.get("mineru_language") or "ch").strip() or "ch"
    model = _normalize_mineru_model(str(settings.get("mineru_model") or ""))
    pages = str(settings.get("mineru_pages") or "").strip()
    use_extract = bool(settings.get("mineru_use_extract", True))
    global_args = _mineru_global_args(settings)
    run_env = _mineru_env(settings)

    warnings: list[str] = []
    saved_assets: list[str] = []
    if len(raw) > MINERU_FLASH_MAX_BYTES and timeout_seconds < 900:
        timeout_seconds = 900.0
        warnings.append("文件较大，已将 MinerU extract 超时提升到 900 秒。")

    def notify(progress: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message, list(warnings))

    with tempfile.TemporaryDirectory(prefix="zric_mineru_") as temp_dir:
        source_name = sanitize_asset_name(filename or f"document{suffix}", suffix or ".bin")
        source_path = os.path.join(temp_dir, source_name)
        output_dir = os.path.join(temp_dir, "out")
        os.makedirs(output_dir, exist_ok=True)
        with open(source_path, "wb") as f:
            f.write(raw)

        attempts, mode_note = _build_mineru_attempts(
            command_parts,
            global_args,
            source_path,
            output_dir,
            suffix=suffix,
            raw_size=len(raw),
            timeout_seconds=timeout_seconds,
            language=language,
            model=model,
            pages=pages,
            ocr_enabled=ocr_enabled,
            use_extract=use_extract,
        )
        if mode_note:
            warnings.append(mode_note)
        if not ocr_enabled:
            warnings.append("已关闭 MinerU OCR；扫描版 PDF 或图片型 Word 可能无法提取正文。")
        if any(mode == "extract" for mode, _args in attempts):
            token_ok, token_warning = _mineru_extract_auth_available(command_parts, run_env)
            if not token_ok:
                warnings.append(token_warning)
                attempts = [(mode, args) for mode, args in attempts if mode != "extract"]
                if not attempts:
                    notify(36, token_warning)
        notify(24, _mineru_attempt_summary(attempts))

        text = ""
        used_mode = ""
        last_error = ""
        for idx, (mode, args) in enumerate(attempts):
            _clear_output_dir(output_dir)
            attempt_base = 24 + min(idx * 5, 10)
            attempt_cap = 36 if idx == len(attempts) - 1 else min(attempt_base + 4, 35)
            notify(attempt_base, f"MinerU {mode} 正在启动")

            def on_process_progress(elapsed_seconds: int, process_timeout_seconds: int, current_mode: str = mode) -> None:
                progress = min(attempt_cap, attempt_base + max(1, elapsed_seconds // 15))
                if elapsed_seconds <= 0:
                    notify(
                        progress,
                        f"MinerU {current_mode} 已启动，等待 MinerU API 返回",
                    )
                    return
                notify(
                    progress,
                    f"MinerU {current_mode} 正在解析（已运行 {elapsed_seconds}s / 超时 {process_timeout_seconds}s）",
                )

            completed, error = _run_mineru(
                args,
                timeout_seconds + 15,
                run_env,
                progress_callback=on_process_progress,
            )
            if error:
                last_error = error
                if mode == "extract":
                    fallback_note = "，将尝试 flash-extract 兜底" if any(m == "flash-extract" for m, _ in attempts[idx + 1:]) else ""
                    warnings.append(f"MinerU extract 失败{fallback_note}：{error}")
                else:
                    fallback_note = "，将尝试 extract 兜底" if any(m == "extract" for m, _ in attempts[idx + 1:]) else ""
                    warnings.append(f"MinerU flash-extract 失败{fallback_note}：{error}")
                notify(attempt_cap, warnings[-1])
                continue
            text = _collect_output_text(output_dir, completed.stdout)
            if text.strip():
                used_mode = mode
                if mode == "flash-extract":
                    warnings.append("当前使用 MinerU flash-extract；该模式通常不导出内嵌图片，超过 10MB/20 页时需配置 token 使用 extract。")
                notify(36, f"MinerU {mode} 已返回文本，正在整理结果")
                break
            last_error = "MinerU 未返回可用文本"
            if mode == "extract" and any(m == "flash-extract" for m, _ in attempts):
                warnings.append("MinerU extract 未返回可用文本，已尝试 flash-extract 兜底。")
            elif mode == "flash-extract" and any(m == "extract" for m, _ in attempts[idx + 1:]):
                warnings.append("MinerU flash-extract 未返回可用文本，已尝试 extract 兜底。")
            notify(attempt_cap, warnings[-1] if warnings else last_error)

        if not text.strip():
            if suffix == ".doc":
                warnings.append("旧式 DOC 需要 MinerU extract 与有效 token；当前未提取到正文。")
            warnings.append(f"MinerU 未提取到正文：{last_error or '未知错误'}")
            notify(36, warnings[-1])
        elif not used_mode:
            used_mode = "mineru"

        if assets_dir:
            saved_assets = _copy_output_images(output_dir, assets_dir)
            if not saved_assets and used_mode == "extract":
                warnings.append("MinerU 未输出可复制的图片资源。")

    return MinerUExtractionResult(text=text.strip(), assets=saved_assets, warnings=warnings, engine=used_mode or "mineru")


def prepare_document_vision_fallback(
    raw: bytes,
    filename: str,
    assets_dir: str | None = None,
    *,
    max_images: int = 12,
) -> VisionFallbackPayload:
    """Prepare local text and page/image snapshots for multimodal OCR fallback."""
    suffix = Path(filename or "").suffix.lower()
    warnings: list[str] = []
    local_text = ""
    candidates: list[_ImageCandidate] = []

    if suffix == ".pdf":
        text, images, pdf_warnings = _extract_pdf_text_and_images(raw, max_images=max_images)
        local_text = text
        candidates.extend(images)
        warnings.extend(pdf_warnings)
    elif suffix == ".docx":
        text, images, docx_warnings = _extract_docx_text_and_images(raw)
        local_text = text
        candidates.extend(images)
        warnings.extend(docx_warnings)
    elif suffix == ".doc":
        warnings.append("旧式 DOC 无法在本地展开为页面图片；如 MinerU 不可用，请先另存为 PDF/DOCX 后重试。")
    else:
        warnings.append("当前文件类型不需要多模态 OCR 兜底。")

    images: list[VisionFallbackImage] = []
    assets: list[str] = []
    seen: set[str] = set()
    skipped_small = 0
    for candidate in candidates:
        digest = hashlib.sha256(candidate.data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        prepared, asset_name, too_small = _prepare_vision_image(candidate, assets_dir)
        if too_small:
            skipped_small += 1
            continue
        if not prepared:
            continue
        images.append(prepared)
        if asset_name:
            assets.append(asset_name)
        if len(images) >= max_images:
            break

    remaining = max(0, len(seen) - skipped_small - len(images))
    if remaining > 0:
        warnings.append(f"多模态兜底最多识别前 {max_images} 张有效页面/图片，已跳过其余 {remaining} 张。")
    if skipped_small:
        warnings.append(f"已跳过 {skipped_small} 张过小图片，避免把图标/装饰当作页面识别。")
    if not images and suffix in {".pdf", ".docx"}:
        warnings.append("未能从文档中提取可供多模态识别的页面图片。")

    return VisionFallbackPayload(
        text=_clean_local_text(local_text),
        images=images,
        assets=assets,
        warnings=warnings,
    )


def _extract_pdf_text_and_images(raw: bytes, *, max_images: int) -> tuple[str, list[_ImageCandidate], list[str]]:
    warnings: list[str] = []
    text = ""
    embedded_images: list[_ImageCandidate] = []
    rendered_pages: list[_ImageCandidate] = []
    with tempfile.TemporaryDirectory(prefix="zric_pdf_local_") as temp_dir:
        source_path = os.path.join(temp_dir, "source.pdf")
        with open(source_path, "wb") as f:
            f.write(raw)

        text, text_warning = _extract_pdf_text_from_file(source_path, temp_dir, max_pages=max_images)
        if text_warning:
            warnings.append(text_warning)
        if text.strip():
            warnings.append(f"PDF 本地兜底已提取 {len(text.strip())} 个文本字符。")

        embedded_images, image_warning = _extract_pdf_embedded_images(source_path, temp_dir, max_images=max_images * 2)
        if image_warning:
            warnings.append(image_warning)
        if embedded_images:
            warnings.append(f"PDF 本地兜底已提取 {len(embedded_images)} 张内嵌图片。")

    rendered_pages, render_warning = _render_pdf_pages_for_vision(raw, max_pages=max_images)
    if render_warning:
        warnings.append(render_warning)
    if rendered_pages:
        warnings.append(f"PDF 本地兜底已渲染前 {len(rendered_pages)} 页为图片。")

    return text, [*rendered_pages, *embedded_images], warnings


def _extract_pdf_text_from_file(source_path: str, output_dir: str, *, max_pages: int) -> tuple[str, str]:
    extractors = (
        _extract_pdf_text_with_pdftotext,
        _extract_pdf_text_with_mutool,
    )
    errors: list[str] = []
    for extractor in extractors:
        text, error = extractor(source_path, output_dir, max_pages)
        if text.strip():
            return text, ""
        if error:
            errors.append(error)
    if errors:
        return "", "PDF 本地文本提取失败：" + "；".join(errors[:2])
    return "", "PDF 本地文本提取需要服务器安装 pdftotext(poppler-utils) 或 mutool。"


def _extract_pdf_text_with_pdftotext(source_path: str, output_dir: str, max_pages: int) -> tuple[str, str]:
    exe = shutil.which("pdftotext")
    if not exe:
        return "", ""
    output_path = os.path.join(output_dir, "pdftotext.txt")
    args = [exe, "-layout", "-enc", "UTF-8", "-f", "1", "-l", str(max_pages), source_path, output_path]
    return _run_pdf_text_extractor(args, output_path, "pdftotext")


def _extract_pdf_text_with_mutool(source_path: str, output_dir: str, max_pages: int) -> tuple[str, str]:
    exe = shutil.which("mutool")
    if not exe:
        return "", ""
    output_path = os.path.join(output_dir, "mutool_text.txt")
    args = [exe, "draw", "-q", "-F", "txt", "-o", output_path, source_path, f"1-{max_pages}"]
    return _run_pdf_text_extractor(args, output_path, "mutool txt")


def _run_pdf_text_extractor(args: list[str], output_path: str, label: str) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{label}: {_short_error(str(exc))}"
    if completed.returncode != 0:
        return "", f"{label}: {_short_error((completed.stderr or completed.stdout or '').strip())}"
    parts: list[str] = []
    if os.path.isfile(output_path):
        try:
            parts.append(Path(output_path).read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            pass
    if completed.stdout.strip():
        parts.append(completed.stdout)
    return _clean_local_text("\n\n".join(parts)), ""


def _extract_pdf_embedded_images(source_path: str, output_dir: str, *, max_images: int) -> tuple[list[_ImageCandidate], str]:
    exe = shutil.which("pdfimages")
    if not exe:
        return [], "PDF 内嵌图片提取需要服务器安装 pdfimages(poppler-utils)。"
    prefix = os.path.join(output_dir, "pdfimage")
    before = {str(path) for path in Path(output_dir).glob("*")}
    args = [exe, "-all", "-f", "1", "-l", str(max_images), source_path, prefix]
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"pdfimages: {_short_error(str(exc))}"
    if completed.returncode != 0:
        return [], f"pdfimages: {_short_error((completed.stderr or completed.stdout or '').strip())}"
    return _image_candidates_from_dir(output_dir, before, label_prefix="PDF 内嵌图片", limit=max_images), ""


def _render_pdf_pages_for_vision(raw: bytes, *, max_pages: int) -> tuple[list[_ImageCandidate], str]:
    renderers = (
        _render_pdf_with_pdftoppm,
        _render_pdf_with_mutool,
        _render_pdf_with_ghostscript,
        _render_pdf_with_magick,
    )
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zric_pdf_vision_") as temp_dir:
        source_path = os.path.join(temp_dir, "source.pdf")
        with open(source_path, "wb") as f:
            f.write(raw)
        for renderer in renderers:
            images, error = renderer(source_path, temp_dir, max_pages)
            if images:
                return images, ""
            if error:
                errors.append(error)
    if errors:
        return [], "PDF 多模态兜底渲染失败：" + "；".join(errors[:3])
    return [], (
        "PDF 多模态兜底需要服务器安装 PDF 渲染器：pdftoppm(poppler-utils)、mutool、Ghostscript 或 ImageMagick。"
        "当前未找到可用渲染器。"
    )


def _render_pdf_with_pdftoppm(source_path: str, output_dir: str, max_pages: int) -> tuple[list[_ImageCandidate], str]:
    exe = shutil.which("pdftoppm")
    if not exe:
        return [], ""
    prefix = os.path.join(output_dir, "pdftoppm_page")
    args = [exe, "-jpeg", "-r", "180", "-f", "1", "-l", str(max_pages), source_path, prefix]
    return _run_pdf_renderer(args, output_dir, "pdftoppm")


def _render_pdf_with_mutool(source_path: str, output_dir: str, max_pages: int) -> tuple[list[_ImageCandidate], str]:
    exe = shutil.which("mutool")
    if not exe:
        return [], ""
    pattern = os.path.join(output_dir, "mutool_page_%03d.jpg")
    args = [exe, "draw", "-q", "-F", "jpg", "-r", "180", "-o", pattern, source_path, f"1-{max_pages}"]
    return _run_pdf_renderer(args, output_dir, "mutool")


def _render_pdf_with_ghostscript(source_path: str, output_dir: str, max_pages: int) -> tuple[list[_ImageCandidate], str]:
    exe = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    if not exe:
        return [], ""
    pattern = os.path.join(output_dir, "gs_page_%03d.jpg")
    args = [
        exe,
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=jpeg",
        "-r180",
        "-dFirstPage=1",
        f"-dLastPage={max_pages}",
        "-dJPEGQ=84",
        f"-sOutputFile={pattern}",
        source_path,
    ]
    return _run_pdf_renderer(args, output_dir, "ghostscript")


def _render_pdf_with_magick(source_path: str, output_dir: str, max_pages: int) -> tuple[list[_ImageCandidate], str]:
    exe = shutil.which("magick")
    if not exe:
        return [], ""
    pattern = os.path.join(output_dir, "magick_page_%03d.jpg")
    args = [exe, "-density", "180", f"{source_path}[0-{max_pages - 1}]", "-quality", "84", pattern]
    return _run_pdf_renderer(args, output_dir, "magick")


def _run_pdf_renderer(args: list[str], output_dir: str, label: str) -> tuple[list[_ImageCandidate], str]:
    before = {str(path) for path in Path(output_dir).glob("*")}
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"{label}: {_short_error(str(exc))}"
    if completed.returncode != 0:
        return [], f"{label}: {_short_error((completed.stderr or completed.stdout or '').strip())}"
    return _image_candidates_from_dir(output_dir, before, label_prefix="PDF 渲染页面"), ""


def _image_candidates_from_dir(
    output_dir: str,
    before: set[str],
    *,
    label_prefix: str,
    limit: int | None = None,
) -> list[_ImageCandidate]:
    images: list[_ImageCandidate] = []
    for path in sorted(Path(output_dir).glob("*"), key=lambda p: p.name.lower()):
        if str(path) in before or not path.is_file() or path.suffix.lower() not in VISION_IMAGE_EXTENSIONS:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data:
            images.append(_ImageCandidate(
                label=f"{label_prefix} {len(images) + 1}",
                filename=path.name,
                data=data,
            ))
        if limit is not None and len(images) >= limit:
            break
    return images


def _extract_docx_text_and_images(raw: bytes) -> tuple[str, list[_ImageCandidate], list[str]]:
    warnings: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return "", [], ["DOCX 本地读取失败：文件不是有效的 DOCX 压缩包。"]

    text_parts: list[str] = []
    try:
        xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for paragraph in root.findall(".//w:p", ns):
            line = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
            if line:
                text_parts.append(line)
    except Exception as exc:
        warnings.append(f"DOCX 本地文本读取失败：{_short_error(str(exc))}")

    images: list[_ImageCandidate] = []
    for name in archive.namelist():
        if not name.startswith("word/media/"):
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        try:
            data = archive.read(name)
        except OSError:
            continue
        if data:
            images.append(_ImageCandidate(
                label=f"DOCX 内嵌图片 {len(images) + 1}",
                filename=Path(name).name,
                data=data,
            ))

    return "\n".join(text_parts).strip(), images, warnings


def _prepare_vision_image(
    candidate: _ImageCandidate,
    assets_dir: str | None,
) -> tuple[VisionFallbackImage | None, str, bool]:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None, "", False

    try:
        with Image.open(io.BytesIO(candidate.data)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if width < 160 or height < 160 or width * height < 80_000:
                return None, "", True
            if image.mode in {"RGBA", "LA", "P"}:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                background.alpha_composite(rgba)
                image = background.convert("RGB")
            elif image.mode != "RGB":
                image = image.convert("RGB")
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            if max(image.size) > 1800:
                image.thumbnail((1800, 1800), resample)
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=84, optimize=True)
            prepared = out.getvalue()
            if len(prepared) > 2_500_000:
                image.thumbnail((1400, 1400), resample)
                out = io.BytesIO()
                image.save(out, format="JPEG", quality=76, optimize=True)
                prepared = out.getvalue()
            final_width, final_height = image.size
    except Exception:
        return None, "", False

    asset_name = ""
    if assets_dir:
        base = Path(candidate.filename or "vision_page.jpg").stem or "vision_page"
        safe = sanitize_asset_name(f"{base}.jpg", ".jpg")
        target = unique_path(assets_dir, safe)
        try:
            with open(target, "wb") as f:
                f.write(prepared)
            asset_name = os.path.basename(target)
        except OSError:
            asset_name = ""

    data_url = "data:image/jpeg;base64," + base64.b64encode(prepared).decode("ascii")
    return (
        VisionFallbackImage(
            label=candidate.label,
            data_url=data_url,
            asset_name=asset_name,
            width=final_width,
            height=final_height,
        ),
        asset_name,
        False,
    )


def _clean_local_text(text: str) -> str:
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()


def _mineru_attempt_summary(attempts: list[tuple[str, list[str]]]) -> str:
    modes = " -> ".join(mode for mode, _args in attempts) or "unknown"
    return f"MinerU 准备解析主剧本文档（模式：{modes}）"


def _build_mineru_attempts(
    command_parts: list[str],
    global_args: list[str],
    source_path: str,
    output_dir: str,
    *,
    suffix: str,
    raw_size: int,
    timeout_seconds: float,
    language: str,
    model: str,
    pages: str,
    ocr_enabled: bool,
    use_extract: bool,
) -> tuple[list[tuple[str, list[str]]], str]:
    def extract_args() -> list[str]:
        return _mineru_extract_args(
            command_parts,
            global_args,
            source_path,
            output_dir,
            timeout_seconds=timeout_seconds,
            language=language,
            model=model,
            pages=pages,
            ocr_enabled=ocr_enabled,
        )

    def flash_args() -> list[str]:
        return _mineru_flash_args(
            command_parts,
            global_args,
            source_path,
            output_dir,
            timeout_seconds=timeout_seconds,
            language=language,
            pages=pages,
            ocr_enabled=ocr_enabled,
        )

    if suffix == ".doc":
        return [("extract", extract_args())], "DOC 仅支持 MinerU extract，需配置有效 token。"

    if not use_extract:
        return [("flash-extract", flash_args())], ""

    if raw_size > MINERU_FLASH_MAX_BYTES:
        return [("extract", extract_args())], "文件超过 MinerU flash-extract 10MB 限制，已自动使用 extract（需要 token）。"

    return [("flash-extract", flash_args()), ("extract", extract_args())], "文件不超过 10MB，已优先使用 flash-extract（免 token）；失败时自动尝试 extract。"


def _command_parts(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = []
    return parts or [command]


def _resolve_mineru_command(command_parts: list[str]) -> tuple[list[str], str]:
    if not command_parts or not str(command_parts[0] or "").strip():
        return [], "mineru_command 为空"

    executable = command_parts[0]
    if _path_like_executable(executable):
        if _executable_available(executable):
            return command_parts, ""
        return [], f"配置的 mineru_command 不存在或当前服务进程不可访问：{executable}"

    resolved = shutil.which(executable)
    if not resolved:
        resolved = shutil.which(executable, path=_expanded_executable_path())
    if resolved:
        return [resolved, *command_parts[1:]], ""
    return [], f"当前服务进程 PATH 未找到 {executable}"


def _path_like_executable(executable: str) -> bool:
    return (
        os.path.isabs(executable)
        or os.sep in executable
        or (os.altsep is not None and os.altsep in executable)
    )


def _expanded_executable_path() -> str:
    raw_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    candidates: list[str] = []
    home = str(Path.home())
    if os.name == "nt":
        candidates.extend([
            os.path.join(home, "AppData", "Roaming", "npm"),
        ])
    else:
        candidates.extend([
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/local/sbin",
            "/usr/sbin",
            "/sbin",
            os.path.join(home, ".local", "bin"),
            os.path.join(home, ".mineru", "bin"),
            os.path.join(home, "go", "bin"),
            "/root/.local/bin",
            "/root/go/bin",
            "/root/.mineru/bin",
            "/opt/mineru/bin",
        ])

    seen: set[str] = set()
    parts: list[str] = []
    for item in [*raw_parts, *candidates]:
        if not item or item in seen:
            continue
        seen.add(item)
        parts.append(item)
    return os.pathsep.join(parts)


def _executable_available(executable: str) -> bool:
    if not executable:
        return False
    if _path_like_executable(executable):
        return os.path.isfile(executable) and (os.name == "nt" or os.access(executable, os.X_OK))
    return shutil.which(executable) is not None


def _mineru_not_found_warning(command: str, reason: str) -> str:
    detail = f"原因：{reason}。" if reason else ""
    return (
        f"当前服务进程找不到 MinerU CLI（{command}），已跳过 MinerU 解析并生成保底内容。"
        f"{detail}"
        "如果使用 1Panel/Docker，请进入运行 Z.R.I.C 的同一个容器执行 `mineru-open-api version` 验证；"
        "宿主机或其他运行环境中安装的 mineru-open-api 不会自动出现在容器内。"
        "请在该容器内安装 MinerU CLI，或把容器内可执行文件的绝对路径写入 config.json 的 campaign_import.mineru_command / 环境变量 ZRIC_MINERU_COMMAND。"
    )


def _normalize_mineru_model(model: str) -> str:
    value = (model or "").strip().lower()
    aliases = {"mineru-html": "html", "mineru_html": "html"}
    value = aliases.get(value, value)
    return value if value in {"vlm", "pipeline", "html"} else ""


def _mineru_global_args(settings: dict[str, Any]) -> list[str]:
    args: list[str] = []
    base_url = str(settings.get("mineru_base_url") or "").strip()
    if base_url:
        args.extend(["--base-url", base_url])
    if settings.get("mineru_verbose"):
        args.append("--verbose")
    return args


def _mineru_env(settings: dict[str, Any]) -> dict[str, str] | None:
    env_overrides: dict[str, str] = {}
    token = str(settings.get("mineru_token") or "").strip()
    if token:
        env_overrides["MINERU_TOKEN"] = token
    source = str(settings.get("mineru_source") or "").strip()
    if source:
        env_overrides["MINERU_SOURCE"] = source
    if not env_overrides:
        return None
    env = os.environ.copy()
    env.update(env_overrides)
    return env


def _mineru_extract_auth_available(command_parts: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Return whether MinerU extract auth appears available without exposing tokens."""
    effective_env = env or os.environ
    if str(effective_env.get("MINERU_TOKEN") or "").strip():
        return True, ""

    auth_cmd = [*command_parts, "auth", "--verify"]
    try:
        completed = subprocess.run(
            auth_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, (
            "MinerU extract 需要 token，但 `mineru-open-api auth --verify` 在 20 秒内未返回。"
            "请在运行 TRPG_Engine 的同一个容器内执行 `mineru-open-api auth --verify` 检查 token 和网络。"
        )
    except OSError as exc:
        return False, f"MinerU extract token 校验失败：{_short_error(str(exc))}"

    if completed.returncode == 0:
        return True, ""

    detail = _short_error((completed.stderr or completed.stdout or "").strip())
    return False, (
        "MinerU extract 需要 token，但当前服务进程未通过 `mineru-open-api auth --verify` 校验。"
        "请在运行 TRPG_Engine 的同一个容器内执行 `mineru-open-api auth`，"
        "或通过容器环境变量 MINERU_TOKEN / ZRIC_MINERU_TOKEN 传入 token。"
        f"校验输出：{detail}"
    )


def _mineru_extract_args(
    command_parts: list[str],
    global_args: list[str],
    source_path: str,
    output_dir: str,
    *,
    timeout_seconds: float,
    language: str,
    model: str,
    pages: str,
    ocr_enabled: bool = True,
) -> list[str]:
    args = [
        *command_parts,
        *global_args,
        "extract",
        source_path,
        "-o",
        output_dir,
        "-f",
        "md,json",
        "--timeout",
        str(int(timeout_seconds)),
    ]
    if ocr_enabled:
        args.append("--ocr")
    if model:
        args.extend(["--model", model])
    if language and language != "ch":
        args.extend(["--language", language])
    if pages:
        args.extend(["--pages", pages])
    return args


def _mineru_flash_args(
    command_parts: list[str],
    global_args: list[str],
    source_path: str,
    output_dir: str,
    *,
    timeout_seconds: float,
    language: str,
    pages: str,
    ocr_enabled: bool = True,
) -> list[str]:
    args = [
        *command_parts,
        *global_args,
        "flash-extract",
        source_path,
        "-o",
        output_dir,
        "--timeout",
        str(int(timeout_seconds)),
    ]
    if ocr_enabled:
        args.append("--ocr")
    if language and language != "ch":
        args.extend(["--language", language])
    if pages:
        args.extend(["--pages", pages])
    return args


def _run_mineru(
    args: list[str],
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    started_at = time.monotonic()
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        dummy = subprocess.CompletedProcess(args, 1, "", str(exc))
        return dummy, _short_error(str(exc))

    if progress_callback:
        progress_callback(0, int(timeout_seconds))

    stdout = ""
    stderr = ""
    while True:
        elapsed = time.monotonic() - started_at
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            try:
                process.kill()
            except OSError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            dummy = subprocess.CompletedProcess(args, 124, stdout or "", stderr or "")
            return dummy, f"超过 {int(timeout_seconds)} 秒未完成"

        try:
            stdout, stderr = process.communicate(timeout=min(2.0, remaining))
            completed = subprocess.CompletedProcess(
                args,
                process.returncode,
                stdout or "",
                stderr or "",
            )
            break
        except subprocess.TimeoutExpired:
            if progress_callback:
                progress_callback(max(1, int(time.monotonic() - started_at)), int(timeout_seconds))

    if completed.returncode != 0:
        return completed, _short_error((completed.stderr or completed.stdout or "").strip())
    return completed, ""


def _clear_output_dir(output_dir: str) -> None:
    root = Path(output_dir)
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        return
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _collect_output_text(output_dir: str, stdout: str = "") -> str:
    root = Path(output_dir)
    md_files = sorted(root.rglob("*.md"), key=lambda p: (len(p.parts), p.name.lower()))
    parts: list[str] = []
    for path in md_files:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)

    json_files = sorted(root.rglob("*.json"), key=lambda p: (len(p.parts), p.name.lower()))
    for path in json_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        extracted = "\n".join(_strings_from_json(data)).strip()
        if extracted:
            return extracted

    cleaned_stdout = _strip_cli_noise(stdout)
    return cleaned_stdout


def _strings_from_json(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        preferred = ("markdown", "md", "text", "content", "html")
        for key in preferred:
            item = value.get(key)
            if isinstance(item, str) and len(item.strip()) > 1:
                texts.append(_strip_html(item) if key == "html" else item.strip())
        for key, item in value.items():
            if key in preferred:
                continue
            texts.extend(_strings_from_json(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_strings_from_json(item))
    return texts


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "\n", text)


def _strip_cli_noise(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    noisy_prefixes = ("saved to", "output", "result", "mineru")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if lines and all(line.lower().startswith(noisy_prefixes) or os.path.exists(line) for line in lines):
        return ""
    return value


def _copy_output_images(output_dir: str, assets_dir: str) -> list[str]:
    saved: list[str] = []
    root = Path(output_dir)
    if not root.exists():
        return saved
    for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        safe = sanitize_asset_name(path.name, path.suffix.lower())
        target = unique_path(assets_dir, safe)
        try:
            shutil.copy2(path, target)
            saved.append(os.path.basename(target))
        except OSError:
            continue
    return saved


def _short_error(text: str) -> str:
    value = re.sub(
        r"(?i)(token|api[-_ ]?key|authorization)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text or "",
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value[:500] or "未知错误"
