#!/usr/bin/env python3
"""
Harbor image backup and retention job for container platforms.

The job copies old Harbor images to Aliyun OSS with skopeo and prunes each old
tag or artifact as soon as its backup is confirmed.
"""

import argparse
import base64
import contextlib
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote as url_quote
from urllib.parse import urlparse

import oss2
import requests
import urllib3


LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
DEFAULT_OSS_PREFIX = "harbor-backups"
DEFAULT_LOCK_FILE = "/tmp/harbor2oss-sync.lock"


@dataclass(frozen=True)
class Config:
    harbor_url: str
    harbor_user: str
    harbor_password: str
    harbor_project: str
    harbor_verify_tls: bool
    harbor_registry: str
    oss_bucket: str
    oss_endpoint: str
    oss_access_key_id: str
    oss_access_key_secret: str
    oss_prefix: str
    keep_tags: int
    tmp_dir: str
    skopeo_override_os: str
    skopeo_override_arch: str
    upload_threads: int
    repo_filter: str | None
    lock_file: str
    network_retries: int
    retry_base_delay_seconds: float
    harbor_request_timeout: float
    log_language: str


@dataclass(frozen=True)
class TagRef:
    name: str
    push_time: datetime


@dataclass(frozen=True)
class ArtifactRef:
    repo_name: str
    repo_path: str
    digest: str
    tags: tuple[TagRef, ...]
    push_time: datetime
    size: int


class JobError(RuntimeError):
    pass


class HarborDeleteForbidden(JobError):
    pass


class NoCookieJar(requests.cookies.RequestsCookieJar):
    def set_cookie(self, cookie, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None


def normalize_log_language(value: str | None) -> str:
    normalized = (value or "en").strip().lower()
    if normalized in {"zh", "zh-cn", "cn", "chinese"}:
        return "zh"
    if normalized in {"en", "en-us", "english"}:
        return "en"
    raise JobError(f"LOG_LANGUAGE must be en or zh, got {value!r}")


def text(config: Config | None, english: str, chinese: str) -> str:
    language = config.log_language if config is not None else normalize_log_language(os.getenv("LOG_LANGUAGE"))
    return chinese if language == "zh" else english


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise JobError(text(None, f"Missing required environment variable: {name}", f"缺少必需的环境变量：{name}"))
    return value or ""


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise JobError(text(None, f"{name} must be an integer, got {raw!r}", f"{name} 必须是整数，当前值为 {raw!r}")) from exc


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise JobError(text(None, f"{name} must be a number, got {raw!r}", f"{name} 必须是数字，当前值为 {raw!r}")) from exc


def load_config(args: argparse.Namespace) -> Config:
    log_language = normalize_log_language(os.getenv("LOG_LANGUAGE"))
    harbor_url = env("HARBOR_URL", required=True).rstrip("/")
    registry = env("HARBOR_REGISTRY", urlparse(harbor_url).netloc, required=True)
    keep_tags = args.keep_tags if args.keep_tags is not None else env_int("KEEP_TAGS", 10)
    if keep_tags < 0:
        raise JobError(text(None, "KEEP_TAGS must be >= 0", "KEEP_TAGS 必须大于等于 0"))
    network_retries = env_int("NETWORK_RETRIES", 3)
    if network_retries < 0:
        raise JobError(text(None, "NETWORK_RETRIES must be >= 0", "NETWORK_RETRIES 必须大于等于 0"))
    retry_base_delay_seconds = env_float("RETRY_BASE_DELAY_SECONDS", 5.0)
    if retry_base_delay_seconds < 0:
        raise JobError(text(None, "RETRY_BASE_DELAY_SECONDS must be >= 0", "RETRY_BASE_DELAY_SECONDS 必须大于等于 0"))
    harbor_request_timeout = env_float("HARBOR_REQUEST_TIMEOUT", 60.0)
    if harbor_request_timeout <= 0:
        raise JobError(text(None, "HARBOR_REQUEST_TIMEOUT must be > 0", "HARBOR_REQUEST_TIMEOUT 必须大于 0"))
    return Config(
        harbor_url=harbor_url,
        harbor_user=env("HARBOR_USER", required=True),
        harbor_password=env("HARBOR_PASSWORD", required=True),
        harbor_project=env("HARBOR_PROJECT", required=True),
        harbor_verify_tls=env_bool("HARBOR_VERIFY_TLS", False),
        harbor_registry=registry,
        oss_bucket=env("OSS_BUCKET", required=True),
        oss_endpoint=env("OSS_ENDPOINT", required=True),
        oss_access_key_id=env("OSS_ACCESS_KEY_ID", required=True),
        oss_access_key_secret=env("OSS_ACCESS_KEY_SECRET", required=True),
        oss_prefix=env("OSS_PREFIX", DEFAULT_OSS_PREFIX).strip("/"),
        keep_tags=keep_tags,
        tmp_dir=env("TMP_DIR", "/tmp/harbor2oss"),
        skopeo_override_os=env("SKOPEO_OVERRIDE_OS", "linux"),
        skopeo_override_arch=env("SKOPEO_OVERRIDE_ARCH", "amd64"),
        upload_threads=env_int("OSS_UPLOAD_THREADS", 4),
        repo_filter=args.repo or os.getenv("REPO_FILTER") or None,
        lock_file=env("LOCK_FILE", DEFAULT_LOCK_FILE),
        network_retries=network_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
        harbor_request_timeout=harbor_request_timeout,
        log_language=log_language,
    )


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logging.warning(text(None, "Unable to parse timestamp %r; treating it as oldest", "无法解析时间戳 %r，将按最旧时间处理"), value)
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def human_size(value: int) -> str:
    n = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def short_digest(digest: str) -> str:
    if digest.startswith("sha256:"):
        return digest[:19]
    return digest[:16]


def create_harbor_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.auth = (config.harbor_user, config.harbor_password)
    session.verify = config.harbor_verify_tls
    session.cookies = NoCookieJar()
    if not config.harbor_verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def retry_delay(config: Config, retry_index: int) -> float:
    return config.retry_base_delay_seconds * (2 ** max(retry_index - 1, 0))


def retryable_harbor_error(exc: requests.exceptions.RequestException) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def response_body_snippet(response: requests.Response | None) -> str:
    if response is None:
        return "<no response>"
    body = (response.text or "").strip().replace("\n", " ")
    return body[:500] if body else "<empty response>"


def api_request(session: requests.Session, config: Config, method: str, url: str, **kwargs) -> requests.Response:
    attempts = config.network_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, timeout=config.harbor_request_timeout, **kwargs)
            if not response.ok:
                raise requests.exceptions.HTTPError(
                    f"{response.status_code} {response.reason} for {method.upper()} {url}; response={response_body_snippet(response)!r}",
                    response=response,
                )
            return response
        except requests.exceptions.RequestException as exc:
            if attempt >= attempts or not retryable_harbor_error(exc):
                raise
            delay = retry_delay(config, attempt)
            logging.warning(
                text(config, "Harbor %s failed on attempt %d/%d: %s; retrying in %.1fs", "Harbor %s 第 %d/%d 次请求失败：%s；%.1f 秒后重试"),
                method.upper(),
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    raise JobError(text(config, f"Harbor {method.upper()} failed unexpectedly: {url}", f"Harbor {method.upper()} 请求异常失败：{url}"))


def api_get(session: requests.Session, config: Config, url: str, **kwargs) -> requests.Response:
    return api_request(session, config, "GET", url, **kwargs)


def api_delete(session: requests.Session, config: Config, url: str, **kwargs) -> requests.Response:
    session.cookies.clear()
    kwargs.setdefault("cookies", {})
    return api_request(session, config, "DELETE", url, **kwargs)


def raise_delete_forbidden(config: Config, target: str, exc: requests.exceptions.HTTPError) -> None:
    response = exc.response
    if response is None or response.status_code != 403:
        return
    raise HarborDeleteForbidden(
        text(
            config,
            f"Harbor denied DELETE for {target}. Check that HARBOR_USER has delete permission in project {config.harbor_project}, and check immutable tag or project policy rules. Harbor response: {response_body_snippet(response)}",
            f"Harbor 拒绝删除 {target}。请确认 HARBOR_USER 在项目 {config.harbor_project} 中有删除权限，并检查不可变 tag 或项目策略。Harbor 返回：{response_body_snippet(response)}",
        )
    ) from exc


def list_repositories(session: requests.Session, config: Config) -> list[str]:
    repos: list[str] = []
    page = 1
    while True:
        response = api_get(
            session,
            config,
            f"{config.harbor_url}/api/v2.0/projects/{config.harbor_project}/repositories",
            params={"page_size": 100, "page": page},
        )
        data = response.json()
        if not data:
            break
        for item in data:
            name = item.get("name")
            if not name:
                continue
            if config.repo_filter and config.repo_filter not in name:
                logging.debug(text(config, "Skip repository %s due to filter %r", "仓库 %s 不匹配过滤条件 %r，跳过"), name, config.repo_filter)
                continue
            repos.append(name)
        logging.debug(text(config, "Fetched repository page %d: %d item(s)", "已获取仓库列表第 %d 页：%d 项"), page, len(data))
        page += 1
    return repos


def repo_path_from_name(config: Config, repo_name: str) -> str:
    prefix = f"{config.harbor_project}/"
    if not repo_name.startswith(prefix):
        raise JobError(text(config, f"Unexpected repository name from Harbor: {repo_name}", f"Harbor 返回了非预期的仓库名：{repo_name}"))
    return repo_name[len(prefix) :]


def list_artifacts(session: requests.Session, config: Config, repo_name: str) -> list[ArtifactRef]:
    repo_path = repo_path_from_name(config, repo_name)
    encoded_repo = url_quote(repo_path, safe="")
    artifacts: list[ArtifactRef] = []
    page = 1
    while True:
        response = api_get(
            session,
            config,
            f"{config.harbor_url}/api/v2.0/projects/{config.harbor_project}/repositories/{encoded_repo}/artifacts",
            params={"page_size": 100, "page": page, "with_tag": "true"},
        )
        data = response.json()
        if not data:
            break
        for raw in data:
            digest = raw.get("digest")
            if not digest:
                continue
            tags = []
            for tag in raw.get("tags") or []:
                name = tag.get("name")
                if not name:
                    continue
                tags.append(TagRef(name=name, push_time=parse_time(tag.get("push_time"))))
            artifacts.append(
                ArtifactRef(
                    repo_name=repo_name,
                    repo_path=repo_path,
                    digest=digest,
                    tags=tuple(tags),
                    push_time=parse_time(raw.get("push_time")),
                    size=int(raw.get("size") or 0),
                )
            )
        logging.debug(text(config, "Fetched artifact page %d for %s: %d item(s)", "已获取制品第 %d 页（仓库 %s）：%d 项"), page, repo_name, len(data))
        page += 1
    return artifacts


def artifact_sort_key(artifact: ArtifactRef) -> datetime:
    if artifact.tags:
        return max(tag.push_time for tag in artifact.tags)
    return artifact.push_time


def artifact_oldest_tag_sort_key(artifact: ArtifactRef) -> datetime:
    if artifact.tags:
        return min(tag.push_time for tag in artifact.tags)
    return artifact.push_time


def iter_tag_backups(artifact: ArtifactRef) -> Iterable[TagRef]:
    return sorted(artifact.tags, key=lambda tag: tag.push_time, reverse=True)


def iter_tag_backups_oldest_first(artifact: ArtifactRef) -> Iterable[TagRef]:
    return sorted(artifact.tags, key=lambda tag: tag.push_time)


def oss_key_for(config: Config, repo_path: str, tag: str) -> str:
    short_name = repo_path.rsplit("/", 1)[-1]
    return f"{config.oss_prefix}/{repo_path}/{short_name}:{tag}.tar"


def init_oss_bucket(config: Config) -> oss2.Bucket:
    auth = oss2.Auth(config.oss_access_key_id, config.oss_access_key_secret)
    bucket = oss2.Bucket(auth, config.oss_endpoint, config.oss_bucket)
    attempts = config.network_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            bucket.get_bucket_info()
            break
        except Exception as exc:
            if attempt >= attempts or not retryable_oss_error(exc):
                raise
            delay = retry_delay(config, attempt)
            logging.warning(
                text(config, "OSS bucket check failed on attempt %d/%d: %s; retrying in %.1fs", "OSS bucket 检查第 %d/%d 次失败：%s；%.1f 秒后重试"),
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    return bucket


def retryable_oss_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, oss2.exceptions.RequestError):
        return True
    if isinstance(exc, oss2.exceptions.OssError):
        status = getattr(exc, "status", None)
        return status is None or status == 429 or status >= 500
    return False


def oss_exists(config: Config, bucket: oss2.Bucket, key: str) -> bool:
    attempts = config.network_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            bucket.head_object(key)
            logging.debug(text(config, "OSS object exists: %s", "OSS 对象已存在：%s"), key)
            return True
        except oss2.exceptions.NotFound:
            logging.debug(text(config, "OSS object is missing: %s", "OSS 对象不存在：%s"), key)
            return False
        except oss2.exceptions.OssError as exc:
            if getattr(exc, "status", None) == 404:
                logging.debug(text(config, "OSS object is missing: %s", "OSS 对象不存在：%s"), key)
                return False
            if attempt >= attempts or not retryable_oss_error(exc):
                raise
            delay = retry_delay(config, attempt)
            logging.warning(
                text(config, "OSS HEAD failed for %s on attempt %d/%d: %s; retrying in %.1fs", "OSS HEAD 检查 %s 第 %d/%d 次失败：%s；%.1f 秒后重试"),
                key,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    raise JobError(text(config, f"OSS HEAD failed unexpectedly for {key}", f"OSS HEAD 检查异常失败：{key}"))


def upload_to_oss(config: Config, bucket: oss2.Bucket, local_path: Path, oss_key: str) -> None:
    attempts = config.network_retries + 1
    size = local_path.stat().st_size
    for attempt in range(1, attempts + 1):
        try:
            if size < 100 * 1024 * 1024:
                logging.debug(text(config, "Using single-part OSS upload for %s", "使用单文件方式上传 OSS：%s"), oss_key)
                result = bucket.put_object_from_file(oss_key, str(local_path))
            else:
                logging.debug(
                    text(config, "Using multipart OSS upload for %s: part_size=%s threads=%d", "使用 OSS 分片上传：%s，分片大小=%s，并发=%d"),
                    oss_key,
                    human_size(10 * 1024 * 1024),
                    config.upload_threads,
                )
                result = oss2.resumable_upload(
                    bucket,
                    oss_key,
                    str(local_path),
                    multipart_threshold=100 * 1024 * 1024,
                    part_size=10 * 1024 * 1024,
                    num_threads=config.upload_threads,
            )
            status = getattr(result, "status", None)
            if status is not None and not (200 <= status < 300):
                if attempt < attempts and (status == 429 or status >= 500):
                    delay = retry_delay(config, attempt)
                    logging.warning(
                        text(config, "OSS upload failed for %s on attempt %d/%d with HTTP %s; retrying in %.1fs", "OSS 上传 %s 第 %d/%d 次失败，HTTP %s；%.1f 秒后重试"),
                        oss_key,
                        attempt,
                        attempts,
                        status,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise JobError(text(config, f"OSS upload failed for {oss_key}: HTTP {status}", f"OSS 上传失败：{oss_key}，HTTP {status}"))
            return
        except Exception as exc:
            if attempt >= attempts or not retryable_oss_error(exc):
                raise
            delay = retry_delay(config, attempt)
            logging.warning(
                text(config, "OSS upload failed for %s on attempt %d/%d: %s; retrying in %.1fs", "OSS 上传 %s 第 %d/%d 次失败：%s；%.1f 秒后重试"),
                oss_key,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)


def write_skopeo_auth(config: Config, directory: Path) -> Path:
    auth_dir = directory / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    token = base64.b64encode(f"{config.harbor_user}:{config.harbor_password}".encode()).decode()
    auth_file = auth_dir / "auth.json"
    auth_file.write_text(
        json.dumps({"auths": {config.harbor_registry: {"auth": token}}}),
        encoding="utf-8",
    )
    auth_file.chmod(0o600)
    logging.debug(text(config, "Wrote temporary skopeo auth file: %s", "已写入临时 skopeo 认证文件：%s"), auth_file)
    return auth_file


def run_skopeo_copy(config: Config, auth_file: Path, image: str, tar_path: Path) -> None:
    command = [
        "skopeo",
        "copy",
        "--authfile",
        str(auth_file),
        f"--src-tls-verify={'true' if config.harbor_verify_tls else 'false'}",
        "--override-os",
        config.skopeo_override_os,
        "--override-arch",
        config.skopeo_override_arch,
        f"docker://{image}",
        f"docker-archive:{tar_path}:{image}",
    ]
    logging.info(text(config, "Exporting image with skopeo: %s", "正在用 skopeo 导出镜像：%s"), image)
    logging.debug(
        text(config, "skopeo copy source=docker://%s target=docker-archive:%s platform=%s/%s tls_verify=%s", "skopeo copy 源=docker://%s 目标=docker-archive:%s 平台=%s/%s tls_verify=%s"),
        image,
        tar_path,
        config.skopeo_override_os,
        config.skopeo_override_arch,
        config.harbor_verify_tls,
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=None)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        raise JobError(text(config, f"skopeo copy failed for {image}: {stderr or stdout}", f"skopeo 导出镜像失败：{image}：{stderr or stdout}"))


def backup_tag(
    config: Config,
    bucket: oss2.Bucket,
    auth_file: Path,
    artifact: ArtifactRef,
    tag: TagRef,
    dry_run: bool,
) -> bool:
    image = f"{config.harbor_registry}/{artifact.repo_name}:{tag.name}"
    key = oss_key_for(config, artifact.repo_path, tag.name)
    logging.debug(
        text(config, "Backup check image=%s digest=%s tag_push_time=%s oss_key=%s", "备份检查 image=%s digest=%s tag_push_time=%s oss_key=%s"),
        image,
        artifact.digest,
        tag.push_time.isoformat(),
        key,
    )
    if oss_exists(config, bucket, key):
        logging.info(text(config, "OSS already has %s", "OSS 已存在：%s"), key)
        return False
    if dry_run:
        logging.info(text(config, "[dry-run] would back up %s -> oss://%s/%s", "[dry-run] 将备份 %s -> oss://%s/%s"), image, config.oss_bucket, key)
        return False

    tmp_root = Path(config.tmp_dir)
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="image_", dir=str(tmp_root)) as work_dir:
        work_path = Path(work_dir)
        tar_path = work_path / "image.tar"
        logging.debug(text(config, "Created temporary work directory for %s: %s", "已为 %s 创建临时工作目录：%s"), image, work_path)
        try:
            run_skopeo_copy(config, auth_file, image, tar_path)
            size = tar_path.stat().st_size
            logging.info(text(config, "Uploading %s (%s) -> oss://%s/%s", "正在上传 %s（%s）-> oss://%s/%s"), image, human_size(size), config.oss_bucket, key)
            upload_to_oss(config, bucket, tar_path, key)
            logging.info(text(config, "Backed up %s", "备份完成：%s"), image)
            return True
        finally:
            with contextlib.suppress(FileNotFoundError):
                tar_path.unlink()
                logging.debug(text(config, "Removed temporary tar for %s: %s", "已删除 %s 的临时 tar：%s"), image, tar_path)


def artifact_fully_backed_up(config: Config, bucket: oss2.Bucket, artifact: ArtifactRef) -> bool:
    if not artifact.tags:
        logging.debug(text(config, "Artifact %s has no tags; treating as not backed up", "制品 %s 没有 tag，按未备份处理"), artifact.digest)
        return False
    for tag in artifact.tags:
        key = oss_key_for(config, artifact.repo_path, tag.name)
        if not oss_exists(config, bucket, key):
            logging.debug(text(config, "Artifact %s is not fully backed up; missing tag %s", "制品 %s 尚未完整备份，缺少 tag %s"), artifact.digest, tag.name)
            return False
    logging.debug(text(config, "Artifact %s is fully backed up", "制品 %s 已完整备份"), artifact.digest)
    return True


def delete_artifact(session: requests.Session, config: Config, artifact: ArtifactRef, dry_run: bool) -> bool:
    encoded_repo = url_quote(artifact.repo_path, safe="")
    encoded_digest = url_quote(artifact.digest, safe="")
    tag_names = ", ".join(tag.name for tag in artifact.tags) or "<untagged>"
    logging.debug(
        text(config, "Delete artifact request repo=%s digest=%s tags=[%s] size=%s", "准备删除制品 repo=%s digest=%s tags=[%s] size=%s"),
        artifact.repo_name,
        artifact.digest,
        tag_names,
        human_size(artifact.size),
    )
    if dry_run:
        logging.info(text(config, "[dry-run] would delete artifact %s/%s@%s tags=[%s]", "[dry-run] 将删除制品 %s/%s@%s tags=[%s]"), config.harbor_project, artifact.repo_path, artifact.digest, tag_names)
        return False
    try:
        api_delete(
            session,
            config,
            f"{config.harbor_url}/api/v2.0/projects/{config.harbor_project}/repositories/{encoded_repo}/artifacts/{encoded_digest}",
        )
    except requests.exceptions.HTTPError as exc:
        raise_delete_forbidden(config, f"{config.harbor_project}/{artifact.repo_path}@{artifact.digest}", exc)
        raise
    logging.info(text(config, "Deleted artifact %s/%s@%s tags=[%s]", "已删除制品 %s/%s@%s tags=[%s]"), config.harbor_project, artifact.repo_path, artifact.digest, tag_names)
    return True


def delete_tag(session: requests.Session, config: Config, artifact: ArtifactRef, tag: TagRef, dry_run: bool) -> bool:
    encoded_repo = url_quote(artifact.repo_path, safe="")
    encoded_digest = url_quote(artifact.digest, safe="")
    encoded_tag = url_quote(tag.name, safe="")
    logging.debug(text(config, "Delete tag request repo=%s digest=%s tag=%s", "准备删除 tag repo=%s digest=%s tag=%s"), artifact.repo_name, artifact.digest, tag.name)
    if dry_run:
        logging.info(text(config, "[dry-run] would delete tag %s/%s:%s", "[dry-run] 将删除 tag %s/%s:%s"), config.harbor_project, artifact.repo_path, tag.name)
        return False
    try:
        api_delete(
            session,
            config,
            f"{config.harbor_url}/api/v2.0/projects/{config.harbor_project}/repositories/{encoded_repo}/artifacts/{encoded_digest}/tags/{encoded_tag}",
        )
    except requests.exceptions.HTTPError as exc:
        raise_delete_forbidden(config, f"{config.harbor_project}/{artifact.repo_path}:{tag.name}", exc)
        raise
    logging.info(text(config, "Deleted tag %s/%s:%s", "已删除 tag %s/%s:%s"), config.harbor_project, artifact.repo_path, tag.name)
    return True


def tag_is_backed_up(config: Config, bucket: oss2.Bucket, artifact: ArtifactRef, tag: TagRef) -> bool:
    return oss_exists(config, bucket, oss_key_for(config, artifact.repo_path, tag.name))


def retention_plan(
    artifacts: list[ArtifactRef],
    keep_tags: int,
) -> tuple[list[ArtifactRef], list[tuple[ArtifactRef, TagRef]], list[tuple[ArtifactRef, TagRef]]]:
    tag_rows: list[tuple[datetime, str, ArtifactRef, TagRef]] = []
    for artifact in artifacts:
        for tag in artifact.tags:
            tag_rows.append((tag.push_time, artifact.digest, artifact, tag))

    tag_rows.sort(key=lambda row: (row[0], row[1], row[3].name), reverse=True)
    retained = {(artifact.digest, tag.name) for _, _, artifact, tag in tag_rows[:keep_tags]}
    logging.debug(text(None, "Retention sees %d tag(s); keeping %d", "保留策略看到 %d 个 tag，将保留 %d 个"), len(tag_rows), len(retained))

    artifact_deletes: list[ArtifactRef] = []
    tag_deletes: list[tuple[ArtifactRef, TagRef]] = []
    retained_tag_rows: list[tuple[ArtifactRef, TagRef]] = []
    for artifact in artifacts:
        if not artifact.tags:
            continue
        artifact_retained_tags = [tag for tag in artifact.tags if (artifact.digest, tag.name) in retained]
        if artifact_retained_tags:
            for tag in artifact.tags:
                if (artifact.digest, tag.name) in retained:
                    retained_tag_rows.append((artifact, tag))
                else:
                    tag_deletes.append((artifact, tag))
        else:
            artifact_deletes.append(artifact)

    artifact_deletes.sort(key=artifact_oldest_tag_sort_key)
    tag_deletes.sort(key=lambda item: item[1].push_time)
    retained_tag_rows.sort(key=lambda item: item[1].push_time)
    logging.debug(
        text(None, "Retention plan: delete_artifacts=%d delete_tags=%d retain_tags=%d", "保留策略计划：删除制品=%d，删除 tag=%d，保留 tag=%d"),
        len(artifact_deletes),
        len(tag_deletes),
        len(retained_tag_rows),
    )
    return artifact_deletes, tag_deletes, retained_tag_rows


def process_repository(
    session: requests.Session,
    config: Config,
    bucket: oss2.Bucket,
    auth_file: Path,
    repo_name: str,
    dry_run: bool,
    prune: bool,
) -> tuple[int, int, int, int]:
    artifacts = list_artifacts(session, config, repo_name)
    tagged_artifacts = [artifact for artifact in artifacts if artifact.tags]
    logging.info(text(config, "%s: %d artifacts, %d tagged", "%s：共 %d 个制品，其中 %d 个带 tag"), repo_name, len(artifacts), len(tagged_artifacts))
    for artifact in sorted(tagged_artifacts, key=artifact_sort_key, reverse=True):
        logging.debug(
            text(config, "%s artifact digest=%s size=%s push_time=%s tags=[%s]", "%s 制品 digest=%s size=%s push_time=%s tags=[%s]"),
            repo_name,
            short_digest(artifact.digest),
            human_size(artifact.size),
            artifact.push_time.isoformat(),
            ", ".join(f"{tag.name}@{tag.push_time.isoformat()}" for tag in iter_tag_backups(artifact)),
        )

    backed_up = 0
    deleted_artifacts = 0
    deleted_tags = 0
    failed = 0

    if not prune:
        for artifact in sorted(tagged_artifacts, key=artifact_sort_key, reverse=True):
            for tag in iter_tag_backups(artifact):
                try:
                    if backup_tag(config, bucket, auth_file, artifact, tag, dry_run):
                        backed_up += 1
                except Exception:
                    failed += 1
                    logging.exception(text(config, "Failed to back up %s:%s", "备份失败：%s:%s"), repo_name, tag.name)
        return backed_up, deleted_artifacts, deleted_tags, failed

    artifact_deletes, tag_deletes, retained_tags = retention_plan(artifacts, config.keep_tags)
    if artifact_deletes or tag_deletes:
        logging.info(
            text(
                config,
                "%s retention plan: delete %d artifact(s), delete %d tag(s), retain %d newest tag(s)",
                "%s 保留策略计划：删除 %d 个制品，删除 %d 个 tag，保留最新 %d 个 tag",
            ),
            repo_name,
            len(artifact_deletes),
            len(tag_deletes),
            len(retained_tags),
        )
    else:
        logging.info(text(config, "%s retention plan: nothing to delete", "%s 保留策略计划：无需删除"), repo_name)

    delete_steps: list[tuple[datetime, str, ArtifactRef, TagRef | None]] = [
        (artifact_oldest_tag_sort_key(artifact), "artifact", artifact, None) for artifact in artifact_deletes
    ]
    delete_steps.extend((tag.push_time, "tag", artifact, tag) for artifact, tag in tag_deletes)
    delete_steps.sort(key=lambda step: (step[0], artifact_oldest_tag_sort_key(step[2]), step[2].digest, step[3].name if step[3] else ""))

    for _, kind, artifact, tag in delete_steps:
        if kind == "tag" and tag is not None:
            try:
                if backup_tag(config, bucket, auth_file, artifact, tag, dry_run):
                    backed_up += 1
                if not dry_run and not tag_is_backed_up(config, bucket, artifact, tag):
                    failed += 1
                    logging.error(
                        text(config, "Skip deleting %s:%s because it is not backed up", "跳过删除 %s:%s，因为尚未完成备份"),
                        repo_name,
                        tag.name,
                    )
                    continue
                if delete_tag(session, config, artifact, tag, dry_run):
                    deleted_tags += 1
            except Exception:
                failed += 1
                logging.exception(text(config, "Failed to back up or delete old tag %s:%s", "备份或删除旧 tag 失败：%s:%s"), repo_name, tag.name)
                if isinstance(sys.exc_info()[1], HarborDeleteForbidden):
                    raise
            continue

        artifact_backup_failed = False
        for old_tag in iter_tag_backups_oldest_first(artifact):
            try:
                if backup_tag(config, bucket, auth_file, artifact, old_tag, dry_run):
                    backed_up += 1
            except Exception:
                failed += 1
                artifact_backup_failed = True
                logging.exception(text(config, "Failed to back up %s:%s", "备份失败：%s:%s"), repo_name, old_tag.name)

        if artifact_backup_failed:
            logging.error(
                text(
                    config,
                    "Skip deleting %s@%s because at least one old tag failed to back up",
                    "跳过删除 %s@%s，因为至少一个旧 tag 备份失败",
                ),
                repo_name,
                artifact.digest,
            )
            continue

        try:
            if not dry_run and not artifact_fully_backed_up(config, bucket, artifact):
                failed += 1
                logging.error(
                    text(config, "Skip deleting %s@%s because not all tags are backed up", "跳过删除 %s@%s，因为不是所有 tag 都已备份"),
                    repo_name,
                    artifact.digest,
                )
                continue
            if delete_artifact(session, config, artifact, dry_run):
                deleted_artifacts += 1
        except Exception:
            failed += 1
            logging.exception(text(config, "Failed to delete old artifact %s@%s", "删除旧制品失败：%s@%s"), repo_name, artifact.digest)
            if isinstance(sys.exc_info()[1], HarborDeleteForbidden):
                raise

    return backed_up, deleted_artifacts, deleted_tags, failed


@contextlib.contextmanager
def lock_file(path: str):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise JobError(text(None, f"Another sync job is already running: {path}", f"已有同步任务正在运行：{path}")) from exc
    try:
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def ensure_tools(dry_run: bool) -> None:
    if dry_run:
        return
    missing = [tool for tool in ("skopeo",) if shutil.which(tool) is None]
    if missing:
        raise JobError(text(None, f"Missing required command: {', '.join(missing)}", f"缺少必需命令：{', '.join(missing)}"))


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level), format=LOG_FORMAT)
    logging.getLogger("oss2").setLevel(logging.WARNING)
    config = load_config(args)
    ensure_tools(args.dry_run)

    with lock_file(config.lock_file):
        started_at = time.time()
        logging.info(text(config, "Starting Harbor -> OSS sync", "开始执行 Harbor -> OSS 同步"))
        logging.info(text(config, "Harbor project: %s", "Harbor 项目：%s"), config.harbor_project)
        logging.info(text(config, "Harbor registry: %s verify_tls=%s", "Harbor 镜像仓库：%s verify_tls=%s"), config.harbor_registry, config.harbor_verify_tls)
        logging.info(text(config, "OSS target: oss://%s/%s/", "OSS 目标：oss://%s/%s/"), config.oss_bucket, config.oss_prefix)
        logging.info(text(config, "Retention: keep latest %d tag(s) per repository", "保留策略：每个仓库保留最新 %d 个 tag"), config.keep_tags)
        logging.info(
            text(config, "Runtime: tmp_dir=%s platform=%s/%s upload_threads=%d repo_filter=%s retries=%d timeout=%.1fs log_language=%s", "运行参数：tmp_dir=%s 平台=%s/%s 上传并发=%d 仓库过滤=%s 重试=%d 超时=%.1fs 日志语言=%s"),
            config.tmp_dir,
            config.skopeo_override_os,
            config.skopeo_override_arch,
            config.upload_threads,
            config.repo_filter or "<none>",
            config.network_retries,
            config.harbor_request_timeout,
            config.log_language,
        )
        logging.debug(text(config, "Lock file: %s", "锁文件：%s"), config.lock_file)
        if args.dry_run:
            logging.info(text(config, "Dry-run mode is enabled", "当前为 dry-run 模式"))

        session = create_harbor_session(config)
        bucket = init_oss_bucket(config)
        tmp_root = Path(config.tmp_dir)
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="job_", dir=str(tmp_root)) as job_dir:
            auth_file = write_skopeo_auth(config, Path(job_dir))
            repos = list_repositories(session, config)
            logging.info(text(config, "Found %d repositories", "发现 %d 个仓库"), len(repos))

            total_backed_up = 0
            total_deleted_artifacts = 0
            total_deleted_tags = 0
            total_failed = 0
            for repo_name in repos:
                backed_up, deleted_artifacts, deleted_tags, failed = process_repository(
                    session=session,
                    config=config,
                    bucket=bucket,
                    auth_file=auth_file,
                    repo_name=repo_name,
                    dry_run=args.dry_run,
                    prune=not args.no_prune,
                )
                total_backed_up += backed_up
                total_deleted_artifacts += deleted_artifacts
                total_deleted_tags += deleted_tags
                total_failed += failed

        elapsed = time.time() - started_at
        logging.info(
            text(config, "Finished in %.1fs: backed_up=%d deleted_artifacts=%d deleted_tags=%d failed=%d", "执行完成，用时 %.1fs：已备份=%d 删除制品=%d 删除 tag=%d 失败=%d"),
            elapsed,
            total_backed_up,
            total_deleted_artifacts,
            total_deleted_tags,
            total_failed,
        )
        return 1 if total_failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up Harbor images to OSS and prune old Harbor artifacts")
    parser.add_argument("--dry-run", action="store_true", help="List work without exporting, uploading, or deleting")
    parser.add_argument("--no-prune", action="store_true", help="Back up images but do not delete old Harbor artifacts")
    parser.add_argument("--repo", help="Only process repositories whose full name contains this text")
    parser.add_argument("--keep-tags", type=int, help="Number of newest tagged artifacts to keep per repository")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log verbosity",
    )
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except KeyboardInterrupt:
        logging.warning(text(None, "Interrupted", "任务已被中断"))
        return 130
    except Exception as exc:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
