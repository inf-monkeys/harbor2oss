#!/usr/bin/env python3
"""
Harbor image backup and retention job for container platforms.

The job copies images from Harbor to Aliyun OSS with skopeo, then prunes old
Harbor artifacts after confirming they are already backed up.
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
DEFAULT_OSS_PREFIX = "Images (backup from harbor.d.run)"
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


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise JobError(f"Missing required environment variable: {name}")
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
        raise JobError(f"{name} must be an integer, got {raw!r}") from exc


def load_config(args: argparse.Namespace) -> Config:
    harbor_url = env("HARBOR_URL", "https://harbor.d.run").rstrip("/")
    registry = env("HARBOR_REGISTRY", urlparse(harbor_url).netloc, required=True)
    keep_tags = args.keep_tags if args.keep_tags is not None else env_int("KEEP_TAGS", 10)
    if keep_tags < 0:
        raise JobError("KEEP_TAGS must be >= 0")
    return Config(
        harbor_url=harbor_url,
        harbor_user=env("HARBOR_USER", required=True),
        harbor_password=env("HARBOR_PASSWORD", required=True),
        harbor_project=env("HARBOR_PROJECT", "infmonkeys"),
        harbor_verify_tls=env_bool("HARBOR_VERIFY_TLS", False),
        harbor_registry=registry,
        oss_bucket=env("OSS_BUCKET", required=True),
        oss_endpoint=env("OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com"),
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
    )


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        logging.warning("Unable to parse timestamp %r; treating as oldest", value)
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
    if not config.harbor_verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def api_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=60, **kwargs)
    response.raise_for_status()
    return response


def list_repositories(session: requests.Session, config: Config) -> list[str]:
    repos: list[str] = []
    page = 1
    while True:
        response = api_get(
            session,
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
                logging.debug("Skip repository %s due to filter %r", name, config.repo_filter)
                continue
            repos.append(name)
        logging.debug("Fetched repository page %d: %d item(s)", page, len(data))
        page += 1
    return repos


def repo_path_from_name(config: Config, repo_name: str) -> str:
    prefix = f"{config.harbor_project}/"
    if not repo_name.startswith(prefix):
        raise JobError(f"Unexpected repository name from Harbor: {repo_name}")
    return repo_name[len(prefix) :]


def list_artifacts(session: requests.Session, config: Config, repo_name: str) -> list[ArtifactRef]:
    repo_path = repo_path_from_name(config, repo_name)
    encoded_repo = url_quote(repo_path, safe="")
    artifacts: list[ArtifactRef] = []
    page = 1
    while True:
        response = api_get(
            session,
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
        logging.debug("Fetched artifact page %d for %s: %d item(s)", page, repo_name, len(data))
        page += 1
    return artifacts


def artifact_sort_key(artifact: ArtifactRef) -> datetime:
    if artifact.tags:
        return max(tag.push_time for tag in artifact.tags)
    return artifact.push_time


def iter_tag_backups(artifact: ArtifactRef) -> Iterable[TagRef]:
    return sorted(artifact.tags, key=lambda tag: tag.push_time, reverse=True)


def oss_key_for(config: Config, repo_path: str, tag: str) -> str:
    short_name = repo_path.rsplit("/", 1)[-1]
    return f"{config.oss_prefix}/{repo_path}/{short_name}:{tag}.tar"


def init_oss_bucket(config: Config) -> oss2.Bucket:
    auth = oss2.Auth(config.oss_access_key_id, config.oss_access_key_secret)
    bucket = oss2.Bucket(auth, config.oss_endpoint, config.oss_bucket)
    bucket.get_bucket_info()
    return bucket


def oss_exists(bucket: oss2.Bucket, key: str) -> bool:
    try:
        bucket.head_object(key)
        logging.debug("OSS exists: %s", key)
        return True
    except oss2.exceptions.NotFound:
        logging.debug("OSS missing: %s", key)
        return False
    except oss2.exceptions.OssError as exc:
        if getattr(exc, "status", None) == 404:
            logging.debug("OSS missing: %s", key)
            return False
        raise


def upload_to_oss(config: Config, bucket: oss2.Bucket, local_path: Path, oss_key: str) -> None:
    size = local_path.stat().st_size
    if size < 100 * 1024 * 1024:
        logging.debug("Using single-part OSS upload for %s", oss_key)
        result = bucket.put_object_from_file(oss_key, str(local_path))
    else:
        logging.debug(
            "Using multipart OSS upload for %s: part_size=%s threads=%d",
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
        raise JobError(f"OSS upload failed for {oss_key}: HTTP {status}")


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
    logging.debug("Wrote temporary skopeo auth file: %s", auth_file)
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
    logging.info("Exporting %s with skopeo", image)
    logging.debug(
        "skopeo copy source=docker://%s target=docker-archive:%s override=%s/%s tls_verify=%s",
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
        raise JobError(f"skopeo copy failed for {image}: {stderr or stdout}")


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
        "Backup check image=%s digest=%s tag_push_time=%s oss_key=%s",
        image,
        artifact.digest,
        tag.push_time.isoformat(),
        key,
    )
    if oss_exists(bucket, key):
        logging.info("OSS already has %s", key)
        return False
    if dry_run:
        logging.info("[dry-run] would back up %s -> oss://%s/%s", image, config.oss_bucket, key)
        return False

    tmp_root = Path(config.tmp_dir)
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="image_", dir=str(tmp_root)) as work_dir:
        work_path = Path(work_dir)
        tar_path = work_path / "image.tar"
        logging.debug("Created temporary work directory for %s: %s", image, work_path)
        try:
            run_skopeo_copy(config, auth_file, image, tar_path)
            size = tar_path.stat().st_size
            logging.info("Uploading %s (%s) -> oss://%s/%s", image, human_size(size), config.oss_bucket, key)
            upload_to_oss(config, bucket, tar_path, key)
            logging.info("Backed up %s", image)
            return True
        finally:
            with contextlib.suppress(FileNotFoundError):
                tar_path.unlink()
                logging.debug("Removed temporary tar for %s: %s", image, tar_path)


def artifact_fully_backed_up(config: Config, bucket: oss2.Bucket, artifact: ArtifactRef) -> bool:
    if not artifact.tags:
        logging.debug("Artifact %s has no tags; treating as not backed up", artifact.digest)
        return False
    for tag in artifact.tags:
        key = oss_key_for(config, artifact.repo_path, tag.name)
        if not oss_exists(bucket, key):
            logging.debug("Artifact %s is not fully backed up; missing tag %s", artifact.digest, tag.name)
            return False
    logging.debug("Artifact %s is fully backed up", artifact.digest)
    return True


def delete_artifact(session: requests.Session, config: Config, artifact: ArtifactRef, dry_run: bool) -> bool:
    encoded_repo = url_quote(artifact.repo_path, safe="")
    encoded_digest = url_quote(artifact.digest, safe="")
    tag_names = ", ".join(tag.name for tag in artifact.tags) or "<untagged>"
    logging.debug(
        "Delete artifact request repo=%s digest=%s tags=[%s] size=%s",
        artifact.repo_name,
        artifact.digest,
        tag_names,
        human_size(artifact.size),
    )
    if dry_run:
        logging.info("[dry-run] would delete artifact %s/%s@%s tags=[%s]", config.harbor_project, artifact.repo_path, artifact.digest, tag_names)
        return False
    response = session.delete(
        f"{config.harbor_url}/api/v2.0/projects/{config.harbor_project}/repositories/{encoded_repo}/artifacts/{encoded_digest}",
        timeout=60,
    )
    response.raise_for_status()
    logging.info("Deleted artifact %s/%s@%s tags=[%s]", config.harbor_project, artifact.repo_path, artifact.digest, tag_names)
    return True


def delete_tag(session: requests.Session, config: Config, artifact: ArtifactRef, tag: TagRef, dry_run: bool) -> bool:
    encoded_repo = url_quote(artifact.repo_path, safe="")
    encoded_digest = url_quote(artifact.digest, safe="")
    encoded_tag = url_quote(tag.name, safe="")
    logging.debug("Delete tag request repo=%s digest=%s tag=%s", artifact.repo_name, artifact.digest, tag.name)
    if dry_run:
        logging.info("[dry-run] would delete tag %s/%s:%s", config.harbor_project, artifact.repo_path, tag.name)
        return False
    response = session.delete(
        f"{config.harbor_url}/api/v2.0/projects/{config.harbor_project}/repositories/{encoded_repo}/artifacts/{encoded_digest}/tags/{encoded_tag}",
        timeout=60,
    )
    response.raise_for_status()
    logging.info("Deleted tag %s/%s:%s", config.harbor_project, artifact.repo_path, tag.name)
    return True


def tag_is_backed_up(config: Config, bucket: oss2.Bucket, artifact: ArtifactRef, tag: TagRef) -> bool:
    return oss_exists(bucket, oss_key_for(config, artifact.repo_path, tag.name))


def retention_plan(artifacts: list[ArtifactRef], keep_tags: int) -> tuple[list[ArtifactRef], list[tuple[ArtifactRef, TagRef]]]:
    tag_rows: list[tuple[datetime, str, ArtifactRef, TagRef]] = []
    for artifact in artifacts:
        for tag in artifact.tags:
            tag_rows.append((tag.push_time, artifact.digest, artifact, tag))

    tag_rows.sort(key=lambda row: (row[0], row[1], row[3].name), reverse=True)
    retained = {(artifact.digest, tag.name) for _, _, artifact, tag in tag_rows[:keep_tags]}
    logging.debug("Retention sees %d tag(s); keeping %d", len(tag_rows), len(retained))

    artifact_deletes: list[ArtifactRef] = []
    tag_deletes: list[tuple[ArtifactRef, TagRef]] = []
    for artifact in artifacts:
        if not artifact.tags:
            continue
        retained_tags = [tag for tag in artifact.tags if (artifact.digest, tag.name) in retained]
        if retained_tags:
            for tag in artifact.tags:
                if (artifact.digest, tag.name) not in retained:
                    tag_deletes.append((artifact, tag))
        else:
            artifact_deletes.append(artifact)

    artifact_deletes.sort(key=artifact_sort_key)
    tag_deletes.sort(key=lambda item: item[1].push_time)
    logging.debug(
        "Retention plan: delete_artifacts=%d delete_tags=%d",
        len(artifact_deletes),
        len(tag_deletes),
    )
    return artifact_deletes, tag_deletes


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
    logging.info("%s: %d artifacts, %d tagged", repo_name, len(artifacts), len(tagged_artifacts))
    for artifact in sorted(tagged_artifacts, key=artifact_sort_key, reverse=True):
        logging.debug(
            "%s artifact digest=%s size=%s push_time=%s tags=[%s]",
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

    for artifact in sorted(tagged_artifacts, key=artifact_sort_key, reverse=True):
        for tag in iter_tag_backups(artifact):
            try:
                if backup_tag(config, bucket, auth_file, artifact, tag, dry_run):
                    backed_up += 1
            except Exception:
                failed += 1
                logging.exception("Failed to back up %s:%s", repo_name, tag.name)

    if not prune:
        return backed_up, deleted_artifacts, deleted_tags, failed

    artifact_deletes, tag_deletes = retention_plan(artifacts, config.keep_tags)
    if artifact_deletes or tag_deletes:
        logging.info(
            "%s retention plan: delete %d artifact(s), delete %d tag(s)",
            repo_name,
            len(artifact_deletes),
            len(tag_deletes),
        )
    else:
        logging.info("%s retention plan: nothing to delete", repo_name)

    for artifact, tag in tag_deletes:
        try:
            if not dry_run and not tag_is_backed_up(config, bucket, artifact, tag):
                failed += 1
                logging.error(
                    "Skip deleting %s:%s because it is not backed up",
                    repo_name,
                    tag.name,
                )
                continue
            if delete_tag(session, config, artifact, tag, dry_run):
                deleted_tags += 1
        except Exception:
            failed += 1
            logging.exception("Failed to delete old tag %s:%s", repo_name, tag.name)

    for artifact in artifact_deletes:
        try:
            if not dry_run and not artifact_fully_backed_up(config, bucket, artifact):
                failed += 1
                logging.error(
                    "Skip deleting %s@%s because not all tags are backed up",
                    repo_name,
                    artifact.digest,
                )
                continue
            if delete_artifact(session, config, artifact, dry_run):
                deleted_artifacts += 1
        except Exception:
            failed += 1
            logging.exception("Failed to delete old artifact %s@%s", repo_name, artifact.digest)

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
        raise JobError(f"Another sync job is already running: {path}") from exc
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
        raise JobError(f"Missing required command: {', '.join(missing)}")


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level), format=LOG_FORMAT)
    logging.getLogger("oss2").setLevel(logging.WARNING)
    config = load_config(args)
    ensure_tools(args.dry_run)

    with lock_file(config.lock_file):
        started_at = time.time()
        logging.info("Starting Harbor -> OSS sync")
        logging.info("Harbor project: %s", config.harbor_project)
        logging.info("Harbor registry: %s verify_tls=%s", config.harbor_registry, config.harbor_verify_tls)
        logging.info("OSS target: oss://%s/%s/", config.oss_bucket, config.oss_prefix)
        logging.info("Retention: keep latest %d tagged artifacts per repository", config.keep_tags)
        logging.info(
            "Runtime: tmp_dir=%s override=%s/%s upload_threads=%d repo_filter=%s",
            config.tmp_dir,
            config.skopeo_override_os,
            config.skopeo_override_arch,
            config.upload_threads,
            config.repo_filter or "<none>",
        )
        logging.debug("Lock file: %s", config.lock_file)
        if args.dry_run:
            logging.info("Dry-run mode is enabled")

        session = create_harbor_session(config)
        bucket = init_oss_bucket(config)
        tmp_root = Path(config.tmp_dir)
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="job_", dir=str(tmp_root)) as job_dir:
            auth_file = write_skopeo_auth(config, Path(job_dir))
            repos = list_repositories(session, config)
            logging.info("Found %d repositories", len(repos))

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
            "Finished in %.1fs: backed_up=%d deleted_artifacts=%d deleted_tags=%d failed=%d",
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
        logging.warning("Interrupted")
        return 130
    except Exception as exc:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
