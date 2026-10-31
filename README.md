# Harbor to OSS Backup

[中文文档](README.zh-CN.md)

Back up tagged images from a Harbor project to Alibaba Cloud OSS as Docker
archive tar files, then optionally prune old Harbor artifacts after confirming
they are present in OSS.

## Features

- Lists repositories in one Harbor project.
- Exports old, prunable image tags with `skopeo` when pruning is enabled.
- Uploads image archives to OSS, using multipart upload for large files.
- Retries transient Harbor and OSS network failures with exponential backoff.
- Keeps the newest `KEEP_TAGS` tags per repository.
- Deletes each old Harbor tag or artifact as soon as its backup is confirmed in OSS.
- Runs as a one-off script or as a container with an in-container cron schedule.
- Supports English logs by default and Chinese logs with `LOG_LANGUAGE=zh`.

## Retention Behavior

The script sorts tags by Harbor `push_time` and keeps the newest `KEEP_TAGS`
tags per repository.

When pruning is enabled, the script plans the retained tags first, then processes
only the old tags and artifacts from oldest to newest. It backs up each old tag
to OSS, confirms the OSS object exists, and immediately deletes the old Harbor
tag or artifact before moving on. This releases Harbor metadata sooner than
waiting for every tag in the repository to be backed up.

If all tags on an old artifact are outside the retention window, the script
deletes the artifact:

```text
DELETE /api/v2.0/projects/{project}/repositories/{repo}/artifacts/{digest}
```

If an artifact has both retained and expired tags, the script only deletes the
expired tags and leaves the artifact itself in place.

## Requirements

- Python 3.12+
- `skopeo`
- A Harbor account with permission to list repositories/artifacts and delete old
  tags/artifacts when pruning is enabled.
- Alibaba Cloud OSS credentials with permission to read and write objects.

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

## Configuration

Required environment variables:

```text
HARBOR_URL
HARBOR_REGISTRY
HARBOR_PROJECT
HARBOR_USER
HARBOR_PASSWORD
OSS_BUCKET
OSS_ENDPOINT
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
```

Optional environment variables:

```text
KEEP_TAGS=10
CRON_SCHEDULE=0 0 * * *
RUN_ON_STARTUP=true
TZ=Asia/Shanghai
TMP_DIR=/tmp/harbor2oss
OSS_PREFIX=harbor-backups
OSS_UPLOAD_THREADS=4
NETWORK_RETRIES=3
RETRY_BASE_DELAY_SECONDS=5
HARBOR_REQUEST_TIMEOUT=60
SKOPEO_OVERRIDE_OS=linux
SKOPEO_OVERRIDE_ARCH=amd64
HARBOR_VERIFY_TLS=false
REPO_FILTER=
JOB_ARGS=
LOG_LEVEL=INFO
LOG_LANGUAGE=en
LOCK_FILE=/tmp/harbor2oss-sync.lock
```

`NETWORK_RETRIES` applies to Harbor API requests and OSS operations. Retries use
exponential backoff starting at `RETRY_BASE_DELAY_SECONDS`.

Set `LOG_LANGUAGE=zh` to print Chinese runtime logs. The default is English.

## Usage

Dry run:

```bash
python3 harbor2oss_sync.py --dry-run
```

Back up without deleting anything:

```bash
python3 harbor2oss_sync.py --no-prune
```

Process repositories whose full Harbor name contains a string:

```bash
python3 harbor2oss_sync.py --repo my-service
```

Keep a different number of tags:

```bash
python3 harbor2oss_sync.py --keep-tags 20
```

Pass arguments to the container job:

```text
JOB_ARGS=--repo my-service --no-prune
```

## Container

Prebuilt multi-architecture images are available on Docker Hub:

```text
ruiruiz30/harbor2oss:latest
```

The image supports `linux/amd64` and `linux/arm64`.

Build the image:

```bash
docker build -t harbor-to-oss-backup .
```

The build compiles `crcmod`'s native C extension for the target architecture in a
separate stage; compilers are not installed in the runtime image. CRC-64 integrity
checks remain enabled. The build fails if the native extension cannot load, the
Python fallback is selected, or the OSS CRC-64 checksum verification fails.

Verify an image without credentials or a network connection:

```bash
docker run --rm --network none --entrypoint python harbor-to-oss-backup /app/verify_native_crc.py
```

Rebuild the image after changing the Python version or target architecture; do
not reuse a compiled extension from a different Python ABI or platform.

Run once:

```bash
docker run --rm --env-file .env ruiruiz30/harbor2oss:latest python /app/harbor2oss_sync.py --no-prune
```

Or with a locally built image:

```bash
docker run --rm --env-file .env harbor-to-oss-backup python /app/harbor2oss_sync.py --no-prune
```

Run with the bundled cron entrypoint:

```bash
docker run --rm --env-file .env ruiruiz30/harbor2oss:latest
```

Or with a locally built image:

```bash
docker run --rm --env-file .env harbor-to-oss-backup
```

The default schedule is:

```text
0 0 * * *
```

The Docker image defaults to `TZ=Asia/Shanghai`.

## Safety Notes

- Do not commit real credentials. Keep them in your deployment environment or a
  local `.env` file.
- Start with `--dry-run` or `--no-prune` before enabling pruning.
- Harbor immutable tags cannot be deleted. The script logs those failures and
  continues.
- Run one job at a time for a given Harbor project and OSS prefix.
- Make sure your container has enough temporary disk space for the largest image
  archive it needs to export.
