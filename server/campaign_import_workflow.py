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
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "step": self.step,
            "message": self.message,
            "warnings": list(self.warnings),
            "result": self.result or {},
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
        extract_docx_text: Callable[[bytes], str],
        extract_docx_images: Callable[[bytes, str], list[str]],
        extract_pdf: Callable[[bytes, str], tuple[str, list[str], list[str]]],
        ai_convert_campaign: Callable[[str, str, list[str]], tuple[dict, dict, list[str]]],
        logger,
        max_jobs: int = 40,
    ) -> None:
        self.campaigns_dir = campaigns_dir
        self.sanitize_campaign_name = sanitize_campaign_name
        self.sanitize_asset_name = sanitize_asset_name
        self.unique_path = unique_path
        self.campaign_asset_url = campaign_asset_url
        self.decode_text_bytes = decode_text_bytes
        self.extract_docx_text = extract_docx_text
        self.extract_docx_images = extract_docx_images
        self.extract_pdf = extract_pdf
        self.ai_convert_campaign = ai_convert_campaign
        self.logger = logger
        self.max_jobs = max_jobs
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
        metadata: dict[str, Any] | None = None,
    ) -> ImportJob:
        self._prune_jobs()
        job = ImportJob(id=uuid.uuid4().hex)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job.id, requested_name, filename, suffix, raw, assets, metadata or {}),
            daemon=True,
        )
        thread.start()
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
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
                job.result = result
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

            self._update(job_id, progress=18, step="extract", message="抽取主剧本文本")
            extracted_assets: list[str] = []
            if suffix == ".pdf":
                text, extracted_assets, pdf_warnings = self.extract_pdf(raw, assets_dir)
                warnings.extend(pdf_warnings)
            elif suffix == ".docx":
                text = self.extract_docx_text(raw)
                self._update(job_id, progress=30, step="extract_assets", message="抽取 DOCX 内嵌图片")
                extracted_assets = self.extract_docx_images(raw, assets_dir)
            else:
                text = self.decode_text_bytes(raw)

            text = (text or "").strip()
            if not text:
                warnings.append("主文件未提取到正文，已生成空白保底剧本。")

            self._update(job_id, progress=38, step="assets", message="保存附加图片资源", warnings=warnings)
            for asset in assets:
                asset_name = asset.filename or ""
                ext = os.path.splitext(asset_name)[1].lower()
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    warnings.append(f"已跳过不支持的图片资源：{asset_name or '未命名文件'}")
                    continue
                if not asset.data:
                    continue
                safe = self.sanitize_asset_name(asset_name)
                target = self.unique_path(assets_dir, safe)
                with open(target, "wb") as f:
                    f.write(asset.data)
                extracted_assets.append(os.path.basename(target))

            self._update(job_id, progress=52, step="convert", message="转换为 Z.R.I.C 可玩剧本", warnings=warnings)
            asset_urls = [self.campaign_asset_url(campaign_name, a) for a in extracted_assets]
            campaign_data, map_data, ai_warnings = self.ai_convert_campaign(campaign_name, text, asset_urls)
            warnings.extend(ai_warnings)

            self._update(job_id, progress=84, step="write", message="写入 campaign.json / map.json / knowledge", warnings=warnings)
            with open(os.path.join(knowledge_dir, "原始剧本文档.txt"), "w", encoding="utf-8") as f:
                f.write(text or "原始文档未提取到文本。")
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
                **metadata,
            }
            with open(os.path.join(folder_path, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

            result = {
                "campaign_path": f"campaigns/{campaign_name}",
                "name": campaign_name,
                "nodes_count": len(campaign_data.get("nodes", [])),
                "map_rooms_count": len(map_data.get("map_rooms", [])),
                "assets_count": len(extracted_assets),
                "manifest": manifest,
            }
            self._update(
                job_id,
                status="success",
                progress=100,
                step="done",
                message="剧本解析完成",
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

    def _is_campaign_child(self, path: str) -> bool:
        root = os.path.realpath(self.campaigns_dir)
        target = os.path.realpath(path)
        try:
            return os.path.commonpath([root, target]) == root and target != root
        except ValueError:
            return False

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
