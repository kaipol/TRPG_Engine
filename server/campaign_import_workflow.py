"""Background campaign-import workflow with progress reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Any
import json
import os
import shutil
import threading
import uuid


@dataclass(frozen=True)
class ImportAsset:
    filename: str
    data: bytes


@dataclass
class ImportJob:
    id: str
    status: str = "queued"
    progress: int = 0
    step: str = "queued"
    message: str = "等待解析"
    warnings: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.result or {})
        result.update({
            "job_id": self.id,
            "status": self.status,
            "progress": self.progress,
            "step": self.step,
            "message": self.message,
            "warnings": list(self.warnings),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        })
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "step": self.step,
            "message": self.message,
            "warnings": list(self.warnings),
            "result": result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class CampaignImportWorkflow:
    """Runs campaign parsing/import as a tracked background job."""

    def __init__(
        self,
        *,
        campaigns_dir: str,
        sanitize_campaign_name: Callable[[str], str],
        sanitize_asset_name: Callable[..., str],
        unique_path: Callable[[str, str], str],
        campaign_asset_url: Callable[[str, str], str],
        decode_text_bytes: Callable[[bytes], str],
        extract_campaign_document: Callable[
            [bytes, str, str, bool, Callable[[int, str, list[str]], None] | None],
            tuple[str, list[str], list[str]],
        ],
        ai_convert_campaign: Callable[[str, str, list[str]], tuple[dict, dict, list[str]]],
        logger,
        multimodal_extract_campaign_document: Callable[
            [bytes, str, str, Callable[[int, str, list[str]], None] | None],
            tuple[str, list[str], list[str]] | tuple[str, list[str], list[str], dict | None],
        ] | None = None,
        convert_structured_campaign_payload: Callable[[str, str, list[str], dict], tuple[dict, dict, list[str]]] | None = None,
        build_knowledge_documents: Callable[[str, str], list[dict[str, str]]] | None = None,
        build_structured_knowledge_documents: Callable[[str, dict, dict], list[dict[str, str]]] | None = None,
        max_jobs: int = 40,
        max_running_seconds: int = 15 * 60,
    ) -> None:
        self.campaigns_dir = campaigns_dir
        self.sanitize_campaign_name = sanitize_campaign_name
        self.sanitize_asset_name = sanitize_asset_name
        self.unique_path = unique_path
        self.campaign_asset_url = campaign_asset_url
        self.decode_text_bytes = decode_text_bytes
        self.extract_campaign_document = extract_campaign_document
        self.multimodal_extract_campaign_document = multimodal_extract_campaign_document
        self.convert_structured_campaign_payload = convert_structured_campaign_payload
        self.ai_convert_campaign = ai_convert_campaign
        self.build_knowledge_documents = build_knowledge_documents or self._fallback_knowledge_documents
        self.build_structured_knowledge_documents = build_structured_knowledge_documents or (lambda _title, _campaign, _map: [])
        self.logger = logger
        self.max_jobs = max_jobs
        self.max_running_seconds = max_running_seconds
        self._jobs: dict[str, ImportJob] = {}
        self._lock = threading.Lock()

    def create_job(
        self,
        *,
        requested_name: str,
        filename: str,
        suffix: str,
        raw: bytes,
        assets: list[ImportAsset],
        ocr_enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> ImportJob:
        self._prune_jobs()
        job = ImportJob(
            id=uuid.uuid4().hex,
            result={
                "source_filename": filename,
                "requested_name": requested_name,
                "suffix": suffix,
                "main_file_size": len(raw),
                "uploaded_assets_count": len(assets),
                "ocr_enabled": bool(ocr_enabled),
            },
        )
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job.id, requested_name, filename, suffix, raw, assets, bool(ocr_enabled), metadata or {}),
            daemon=True,
        )
        thread.start()
        return job

    def create_reparse_job(
        self,
        *,
        campaign_name: str,
        folder_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ImportJob:
        self._prune_jobs()
        job = ImportJob(
            id=uuid.uuid4().hex,
            result={
                "name": campaign_name,
                "campaign_path": f"campaigns/{campaign_name}",
                "mode": "reparse",
            },
        )
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run_reparse_job,
            args=(job.id, campaign_name, folder_path, metadata or {}),
            daemon=True,
        )
        thread.start()
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status in {"queued", "running"} and self._job_is_stale(job):
                job.status = "error"
                job.progress = 100
                job.step = "error"
                job.message = "剧本解析超时"
                job.error = "剧本解析长时间无进展，已停止等待；请检查 MinerU 配置、网络连接，或降低导入文件复杂度后重试。"
                job.updated_at = datetime.now().isoformat(timespec="seconds")
            return job.to_dict() if job else None

    def _update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        step: str | None = None,
        message: str | None = None,
        warnings: list[str] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {"success", "error"} and status != job.status:
                return
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = max(0, min(100, int(progress)))
            if step is not None:
                job.step = step
            if message is not None:
                job.message = message
            if warnings is not None:
                job.warnings = warnings
            if result is not None:
                current = job.result if isinstance(job.result, dict) else {}
                job.result = {**current, **result}
            if error is not None:
                job.error = error
            job.updated_at = datetime.now().isoformat(timespec="seconds")

    def _run_job(
        self,
        job_id: str,
        requested_name: str,
        filename: str,
        suffix: str,
        raw: bytes,
        assets: list[ImportAsset],
        ocr_enabled: bool,
        metadata: dict[str, Any],
    ) -> None:
        folder_path = ""
        folder_created = False
        warnings: list[str] = []
        try:
            self._update(job_id, status="running", progress=5, step="prepare", message="准备剧本目录")
            base_campaign_name = self.sanitize_campaign_name(requested_name or os.path.splitext(filename)[0])
            campaign_name = base_campaign_name
            folder_path = os.path.join(self.campaigns_dir, campaign_name)
            if os.path.exists(folder_path):
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                campaign_name = f"{base_campaign_name}_{stamp}"
                folder_path = os.path.join(self.campaigns_dir, campaign_name)
                i = 1
                while os.path.exists(folder_path):
                    campaign_name = f"{base_campaign_name}_{stamp}_{i}"
                    folder_path = os.path.join(self.campaigns_dir, campaign_name)
                    i += 1
            knowledge_dir = os.path.join(folder_path, "knowledge")
            assets_dir = os.path.join(folder_path, "assets")
            os.makedirs(folder_path, exist_ok=False)
            folder_created = True
            os.makedirs(knowledge_dir, exist_ok=True)
            os.makedirs(assets_dir, exist_ok=True)
            self._update(
                job_id,
                result={
                    "name": campaign_name,
                    "campaign_path": f"campaigns/{campaign_name}",
                    "knowledge_path": f"campaigns/{campaign_name}/knowledge",
                    "assets_path": f"campaigns/{campaign_name}/assets",
                },
            )

            self._update(job_id, progress=18, step="extract", message="解析主剧本文档")
            extracted_assets: list[str] = []
            post_extract_progress = 38
            if suffix in {".pdf", ".docx", ".doc"}:
                extract_message = "MinerU OCR 解析主剧本文档" if ocr_enabled else "MinerU 解析主剧本文档（OCR 已关闭）"
                self._update(job_id, progress=24, step="ocr", message=extract_message, warnings=warnings)

                def on_mineru_progress(progress: int, message: str, mineru_warnings: list[str]) -> None:
                    for item in mineru_warnings:
                        if item and item not in warnings:
                            warnings.append(item)
                    self._update(
                        job_id,
                        progress=progress,
                        step="ocr",
                        message=message,
                        warnings=list(warnings),
                        result={
                            "mineru_progress": progress,
                            "mineru_message": message,
                            "mineru_warnings_count": len(warnings),
                        },
                    )

                text, extracted_assets, mineru_warnings = self.extract_campaign_document(
                    raw,
                    filename,
                    assets_dir,
                    ocr_enabled,
                    on_mineru_progress,
                )
                for item in mineru_warnings:
                    if item and item not in warnings:
                        warnings.append(item)
            else:
                self._update(job_id, progress=30, step="extract", message="读取纯文本剧本", warnings=warnings)
                text = self.decode_text_bytes(raw)

            text = (text or "").strip()
            structured_payload: dict[str, Any] | None = None
            if not text and suffix in {".pdf", ".docx", ".doc"} and self.multimodal_extract_campaign_document:
                self._update(
                    job_id,
                    progress=39,
                    step="vision",
                    message="MinerU 未提取正文，尝试多模态识别兜底",
                    warnings=warnings,
                )

                def on_vision_progress(progress: int, message: str, vision_warnings: list[str]) -> None:
                    for item in vision_warnings:
                        if item and item not in warnings:
                            warnings.append(item)
                    self._update(
                        job_id,
                        progress=progress,
                        step="vision",
                        message=message,
                        warnings=list(warnings),
                        result={
                            "vision_progress": progress,
                            "vision_message": message,
                            "vision_warnings_count": len(warnings),
                        },
                    )

                vision_result = self.multimodal_extract_campaign_document(
                    raw,
                    filename,
                    assets_dir,
                    on_vision_progress,
                )
                if len(vision_result) == 4:
                    vision_text, vision_assets, vision_warnings, structured_payload = vision_result
                else:
                    vision_text, vision_assets, vision_warnings = vision_result
                text = (vision_text or "").strip()
                for item in vision_warnings:
                    if item and item not in warnings:
                        warnings.append(item)
                for asset_name in vision_assets:
                    if asset_name and asset_name not in extracted_assets:
                        extracted_assets.append(asset_name)
                post_extract_progress = 56

            if not text:
                warnings.append("主文件未提取到正文；多模态兜底未获得可用文本，已生成空白保底剧本。")

            self._update(job_id, progress=post_extract_progress, step="assets", message="保存附加图片资源", warnings=warnings)
            for asset in assets:
                asset_name = asset.filename or ""
                ext = os.path.splitext(asset_name)[1].lower()
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                    warnings.append(f"已跳过不支持的图片资源：{asset_name or '未命名文件'}")
                    continue
                if not asset.data:
                    continue
                safe = self.sanitize_asset_name(asset_name)
                target = self.unique_path(assets_dir, safe)
                with open(target, "wb") as f:
                    f.write(asset.data)
                extracted_assets.append(os.path.basename(target))

            self._update(job_id, progress=max(52, post_extract_progress + 4), step="convert", message="转换为 Z.R.I.C 可玩剧本", warnings=warnings)
            asset_urls = [self.campaign_asset_url(campaign_name, a) for a in extracted_assets]
            if structured_payload and self.convert_structured_campaign_payload:
                campaign_data, map_data, ai_warnings = self.convert_structured_campaign_payload(campaign_name, text, asset_urls, structured_payload)
            else:
                campaign_data, map_data, ai_warnings = self.ai_convert_campaign(campaign_name, text, asset_urls)
            warnings.extend(ai_warnings)

            self._update(job_id, progress=84, step="write", message="写入 campaign.json / map.json / 分段知识库", warnings=warnings)
            knowledge_files = self._write_knowledge_documents(
                knowledge_dir,
                campaign_name,
                text or "原始文档未提取到文本。",
                extra_documents=self.build_structured_knowledge_documents(campaign_name, campaign_data, map_data),
            )
            with open(os.path.join(folder_path, "campaign.json"), "w", encoding="utf-8") as f:
                json.dump(campaign_data, f, ensure_ascii=False, indent=4)
            with open(os.path.join(folder_path, "map.json"), "w", encoding="utf-8") as f:
                json.dump(map_data, f, ensure_ascii=False, indent=4)
            manifest = {
                "name": campaign_name,
                "path": f"campaigns/{campaign_name}",
                "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_filename": filename,
                "node_count": len(campaign_data.get("nodes", [])),
                "character_count": len(campaign_data.get("characters", [])),
                "map_room_count": len(map_data.get("map_rooms") or []),
                "asset_count": len(extracted_assets),
                "knowledge_count": len(knowledge_files),
                "knowledge_files": knowledge_files,
                "parse_version": 2,
                "visibility": "public",
                "ocr_enabled": bool(ocr_enabled),
                "main_file_size": len(raw),
                **metadata,
            }
            with open(os.path.join(folder_path, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            self._update(job_id, progress=94, step="verify", message="校验导入结果", warnings=warnings)
            verification = self._verify_import_result(folder_path, knowledge_dir)
            manifest["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            manifest["verification"] = verification
            with open(os.path.join(folder_path, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            result = {
                "campaign_path": f"campaigns/{campaign_name}",
                "name": campaign_name,
                "nodes_count": len(campaign_data.get("nodes", [])),
                "characters_count": len(campaign_data.get("characters", [])),
                "lore_count": len(campaign_data.get("lorebook", [])),
                "entities_count": len(campaign_data.get("world_entities", [])),
                "map_rooms_count": len(map_data.get("map_rooms", [])),
                "assets_count": len(extracted_assets),
                "knowledge_count": len(knowledge_files),
                "import_verified": True,
                "verification": verification,
                "manifest": manifest,
            }
            self._update(
                job_id,
                status="success",
                progress=100,
                step="done",
                message="剧本解析完成，已通过导入校验",
                warnings=warnings,
                result=result,
            )
        except Exception as exc:
            if folder_created and folder_path and self._is_campaign_child(folder_path):
                shutil.rmtree(folder_path, ignore_errors=True)
            detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
            self.logger.error("campaign import workflow failed: %s", detail, exc_info=True)
            self._update(
                job_id,
                status="error",
                progress=100,
                step="error",
                message="剧本解析失败",
                warnings=warnings,
                error=str(detail),
            )

    def _run_reparse_job(
        self,
        job_id: str,
        campaign_name: str,
        folder_path: str,
        metadata: dict[str, Any],
    ) -> None:
        warnings: list[str] = []
        backup: dict[str, bytes | None] = {}
        try:
            if not self._is_campaign_child(folder_path):
                raise RuntimeError("非法剧本目录")
            knowledge_dir = os.path.join(folder_path, "knowledge")
            assets_dir = os.path.join(folder_path, "assets")
            os.makedirs(knowledge_dir, exist_ok=True)
            os.makedirs(assets_dir, exist_ok=True)
            self._update(job_id, status="running", progress=8, step="read", message="读取原始 OCR 文本")
            text = self._read_reparse_source_text(folder_path, knowledge_dir)
            if not text.strip():
                raise RuntimeError("未找到可重新识别的原始文本；请重新上传剧本文档")

            asset_names = [
                name for name in sorted(os.listdir(assets_dir))
                if os.path.splitext(name)[1].lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                and os.path.isfile(os.path.join(assets_dir, name))
            ]
            asset_urls = [self.campaign_asset_url(campaign_name, name) for name in asset_names]
            self._update(job_id, progress=30, step="convert", message="重新结构化剧本、角色、地图与知识库", warnings=warnings)
            campaign_data, map_data, ai_warnings = self.ai_convert_campaign(campaign_name, text, asset_urls)
            warnings.extend(ai_warnings)

            for filename in ("campaign.json", "map.json", "manifest.json"):
                path = os.path.join(folder_path, filename)
                backup[path] = open(path, "rb").read() if os.path.isfile(path) else None

            manifest_path = os.path.join(folder_path, "manifest.json")
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    previous_manifest = json.load(f)
                if not isinstance(previous_manifest, dict):
                    previous_manifest = {}
            except (OSError, json.JSONDecodeError):
                previous_manifest = {}

            self._update(job_id, progress=78, step="write", message="写入重新识别结果", warnings=warnings)
            knowledge_files = self._write_knowledge_documents(
                knowledge_dir,
                campaign_name,
                text,
                previous_manifest=previous_manifest,
                extra_documents=self.build_structured_knowledge_documents(campaign_name, campaign_data, map_data),
            )
            with open(os.path.join(folder_path, "campaign.json"), "w", encoding="utf-8") as f:
                json.dump(campaign_data, f, ensure_ascii=False, indent=4)
            with open(os.path.join(folder_path, "map.json"), "w", encoding="utf-8") as f:
                json.dump(map_data, f, ensure_ascii=False, indent=4)

            manifest = {
                **previous_manifest,
                **metadata,
                "name": campaign_name,
                "path": f"campaigns/{campaign_name}",
                "reparsed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "node_count": len(campaign_data.get("nodes", [])),
                "character_count": len(campaign_data.get("characters", [])),
                "map_room_count": len(map_data.get("map_rooms") or []),
                "asset_count": len(asset_names),
                "knowledge_count": len(knowledge_files),
                "knowledge_files": knowledge_files,
                "parse_version": 2,
                "visibility": previous_manifest.get("visibility") or "public",
            }
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            self._update(job_id, progress=94, step="verify", message="校验重新识别结果", warnings=warnings)
            verification = self._verify_import_result(folder_path, knowledge_dir)
            manifest["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            manifest["verification"] = verification
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            self._update(
                job_id,
                status="success",
                progress=100,
                step="done",
                message="剧本重新识别完成，已通过导入校验",
                warnings=warnings,
                result={
                    "campaign_path": f"campaigns/{campaign_name}",
                    "name": campaign_name,
                    "mode": "reparse",
                    "nodes_count": len(campaign_data.get("nodes", [])),
                    "characters_count": len(campaign_data.get("characters", [])),
                    "lore_count": len(campaign_data.get("lorebook", [])),
                    "entities_count": len(campaign_data.get("world_entities", [])),
                    "map_rooms_count": len(map_data.get("map_rooms", [])),
                    "assets_count": len(asset_names),
                    "knowledge_count": len(knowledge_files),
                    "import_verified": True,
                    "verification": verification,
                    "manifest": manifest,
                },
            )
        except Exception as exc:
            for path, data in backup.items():
                try:
                    if data is None:
                        if os.path.exists(path):
                            os.remove(path)
                    else:
                        with open(path, "wb") as f:
                            f.write(data)
                except OSError:
                    pass
            detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
            self.logger.error("campaign reparse workflow failed: %s", detail, exc_info=True)
            self._update(
                job_id,
                status="error",
                progress=100,
                step="error",
                message="剧本重新识别失败",
                warnings=warnings,
                error=str(detail),
            )

    def _is_campaign_child(self, path: str) -> bool:
        root = os.path.realpath(self.campaigns_dir)
        target = os.path.realpath(path)
        try:
            return os.path.commonpath([root, target]) == root and target != root
        except ValueError:
            return False

    def _fallback_knowledge_documents(self, _title: str, text: str) -> list[dict[str, str]]:
        return [{
            "title": "原始剧本文档",
            "filename": "原始剧本文档.txt",
            "text": text or "原始文档未提取到文本。",
        }]

    def _safe_knowledge_filename(self, filename: str, index: int) -> str:
        stem, ext = os.path.splitext(filename or "")
        ext = ext if ext.lower() in {".txt", ".md"} else ".md"
        safe_stem = self.sanitize_asset_name(stem or f"knowledge_{index:02d}", "")
        safe_stem = os.path.splitext(safe_stem)[0] or f"knowledge_{index:02d}"
        return f"{safe_stem[:56]}{ext}"

    def _write_knowledge_documents(
        self,
        knowledge_dir: str,
        campaign_name: str,
        text: str,
        previous_manifest: dict[str, Any] | None = None,
        extra_documents: list[dict[str, str]] | None = None,
    ) -> list[str]:
        os.makedirs(knowledge_dir, exist_ok=True)
        previous_files = previous_manifest.get("knowledge_files") if isinstance(previous_manifest, dict) else []
        if isinstance(previous_files, list):
            for filename in previous_files:
                if filename == "原始剧本文档.txt":
                    continue
                path = os.path.realpath(os.path.join(knowledge_dir, str(filename)))
                try:
                    if os.path.commonpath([os.path.realpath(knowledge_dir), path]) == os.path.realpath(knowledge_dir) and os.path.isfile(path):
                        os.remove(path)
                except (OSError, ValueError):
                    pass

        docs = [
            *self.build_knowledge_documents(campaign_name, text),
            *(extra_documents or []),
        ]
        if not any(doc.get("filename") == "原始剧本文档.txt" for doc in docs if isinstance(doc, dict)):
            docs.insert(0, {
                "title": "原始剧本文档",
                "filename": "原始剧本文档.txt",
                "text": text or "原始文档未提取到文本。",
            })

        written: list[str] = []
        used: set[str] = set()
        for idx, doc in enumerate(docs[:64], start=1):
            if not isinstance(doc, dict):
                continue
            filename = self._safe_knowledge_filename(str(doc.get("filename") or ""), idx)
            if filename == "原始剧本文档.md":
                filename = "原始剧本文档.txt"
            base, ext = os.path.splitext(filename)
            candidate = filename
            suffix = 1
            while candidate in used:
                candidate = f"{base}_{suffix}{ext}"
                suffix += 1
            used.add(candidate)
            body = str(doc.get("text") or "").strip()
            if not body:
                continue
            with open(os.path.join(knowledge_dir, candidate), "w", encoding="utf-8") as f:
                f.write(body)
            written.append(candidate)
        return written

    def _read_reparse_source_text(self, folder_path: str, knowledge_dir: str) -> str:
        preferred = os.path.join(knowledge_dir, "原始剧本文档.txt")
        if os.path.isfile(preferred):
            try:
                with open(preferred, "r", encoding="utf-8") as f:
                    text = f.read().strip()
                if text:
                    return text
            except OSError:
                pass

        parts: list[str] = []
        if os.path.isdir(knowledge_dir):
            for filename in sorted(os.listdir(knowledge_dir)):
                if os.path.splitext(filename)[1].lower() not in {".txt", ".md"}:
                    continue
                path = os.path.join(knowledge_dir, filename)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        text = f.read().strip()
                    if text:
                        parts.append(f"# {os.path.splitext(filename)[0]}\n\n{text}")
                except OSError:
                    continue
        if parts:
            return "\n\n".join(parts)

        campaign_json = os.path.join(folder_path, "campaign.json")
        try:
            with open(campaign_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                node_parts = []
                for node in data.get("nodes", []):
                    if isinstance(node, dict):
                        node_parts.append(
                            f"# {node.get('name') or '场景'}\n\n"
                            f"{node.get('summary') or ''}\n\n{node.get('content') or ''}"
                        )
                return "\n\n".join(part for part in node_parts if part.strip())
        except (OSError, json.JSONDecodeError):
            pass
        return ""

    def _verify_import_result(self, folder_path: str, knowledge_dir: str) -> dict[str, Any]:
        checks: list[str] = []
        errors: list[str] = []

        if not self._is_campaign_child(folder_path):
            errors.append("导入目录不在 campaigns 安全目录内")
        elif os.path.isdir(folder_path):
            checks.append("campaigns 子目录已创建")
        else:
            errors.append("导入目录不存在")

        campaign_json = os.path.join(folder_path, "campaign.json")
        campaign_data: Any = None
        if os.path.isfile(campaign_json):
            try:
                with open(campaign_json, "r", encoding="utf-8") as f:
                    campaign_data = json.load(f)
                checks.append("campaign.json 可读取")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"campaign.json 无法读取：{exc}")
        else:
            errors.append("campaign.json 未写入")

        node_count = 0
        character_count = 0
        if isinstance(campaign_data, dict):
            nodes = campaign_data.get("nodes")
            characters = campaign_data.get("characters")
            if isinstance(nodes, list):
                node_count = len(nodes)
                checks.append("campaign.json 包含 nodes 列表")
            else:
                errors.append("campaign.json 缺少 nodes 列表")
            if isinstance(characters, list):
                character_count = len(characters)
        elif campaign_data is not None:
            errors.append("campaign.json 根结构不是对象")

        map_json = os.path.join(folder_path, "map.json")
        map_room_count = 0
        if os.path.isfile(map_json):
            try:
                with open(map_json, "r", encoding="utf-8") as f:
                    map_data = json.load(f)
                if isinstance(map_data, dict):
                    rooms = map_data.get("map_rooms") or []
                    if isinstance(rooms, list):
                        map_room_count = len(rooms)
                    checks.append("map.json 可读取")
                else:
                    errors.append("map.json 根结构不是对象")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"map.json 无法读取：{exc}")
        else:
            errors.append("map.json 未写入")

        knowledge_file = os.path.join(knowledge_dir, "原始剧本文档.txt")
        if os.path.isfile(knowledge_file) and os.path.getsize(knowledge_file) > 0:
            checks.append("knowledge 原文文档已写入")
        else:
            errors.append("knowledge 原文文档为空或未写入")

        assets_dir = os.path.join(folder_path, "assets")
        assets_count = len(os.listdir(assets_dir)) if os.path.isdir(assets_dir) else 0
        if os.path.isdir(assets_dir):
            checks.append("assets 目录已创建")
        else:
            errors.append("assets 目录未创建")

        verification = {
            "ok": not errors,
            "checks": checks,
            "errors": errors,
            "nodes_count": node_count,
            "characters_count": character_count,
            "map_rooms_count": map_room_count,
            "assets_count": assets_count,
        }
        if errors:
            raise RuntimeError("导入校验失败：" + "；".join(errors))
        return verification

    def _job_is_stale(self, job: ImportJob) -> bool:
        try:
            updated = datetime.fromisoformat(job.updated_at)
        except ValueError:
            updated = datetime.now()
        return (datetime.now() - updated).total_seconds() > self.max_running_seconds

    def _prune_jobs(self) -> None:
        with self._lock:
            if len(self._jobs) < self.max_jobs:
                return
            cutoff = datetime.now() - timedelta(hours=6)
            removable = []
            for job_id, job in self._jobs.items():
                try:
                    updated = datetime.fromisoformat(job.updated_at)
                except ValueError:
                    updated = datetime.now()
                if job.status in {"success", "error"} and updated < cutoff:
                    removable.append(job_id)
            for job_id in removable:
                self._jobs.pop(job_id, None)
            while len(self._jobs) >= self.max_jobs:
                oldest = min(self._jobs.values(), key=lambda j: j.updated_at)
                if oldest.status == "running":
                    break
                self._jobs.pop(oldest.id, None)
