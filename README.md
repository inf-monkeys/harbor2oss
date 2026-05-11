# Harbor to OSS Zeabur Job

Container job for backing up Harbor images to Aliyun OSS and pruning old Harbor
artifacts.

## What It Does

- Lists all repositories in one Harbor project.
- Backs up every missing tag to OSS as a Docker archive tar.
- Uses `skopeo`, so the container does not need Docker or privileged mode.
- Deletes each temporary tar immediately after upload.
- Keeps the newest `KEEP_TAGS` tags per repository.
- Deletes old Harbor artifacts after all of their tags are confirmed in OSS.
- Uses an in-container cron scheduler for daily runs.

## Retention Behavior

The script sorts tags by Harbor `push_time` and keeps the newest `KEEP_TAGS`
tags per repository.

If all tags on an old artifact are outside the retention window, the script
deletes the artifact:

```text
DELETE /api/v2.0/projects/{project}/repositories/{repo}/artifacts/{digest}
```

That is the behavior that released quota in the Harbor UI during testing. If an
artifact has both retained and expired tags, the script only deletes the expired
tags and leaves the artifact itself in place.

## Zeabur Setup

1. Create a new Zeabur service from this directory.
2. Use Dockerfile deployment.
3. Set the environment variables from `.env.example`.
4. Keep replicas at `1`, so two sync jobs cannot run at the same time.

Default schedule:

```text
0 0 * * *
```

That means 00:00 every day in the container timezone. The Docker image defaults
to `TZ=Asia/Shanghai`.

## Required Environment Variables

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

Optional variables:

```text
KEEP_TAGS=10
CRON_SCHEDULE=0 0 * * *
RUN_ON_STARTUP=true
TZ=Asia/Shanghai
TMP_DIR=/tmp/harbor2oss
OSS_PREFIX=Images (backup from harbor.d.run)
OSS_UPLOAD_THREADS=4
NETWORK_RETRIES=3
RETRY_BASE_DELAY_SECONDS=5
HARBOR_REQUEST_TIMEOUT=60
SKOPEO_OVERRIDE_OS=linux
SKOPEO_OVERRIDE_ARCH=amd64
HARBOR_VERIFY_TLS=false
REPO_FILTER=
JOB_ARGS=
LOG_LEVEL=DEBUG
```

`NETWORK_RETRIES` applies to Harbor API requests and OSS uploads. Retries use
exponential backoff starting at `RETRY_BASE_DELAY_SECONDS`.

## Local Dry Run

Install dependencies and `skopeo`, then run:

```bash
python3 -m pip install -r requirements.txt
python3 harbor2oss_sync.py --dry-run --repo monkeys-studio
```

Dry run still connects to Harbor and OSS to verify real state, but it does not
export, upload, delete tags, or delete artifacts.

## Useful One-Off Commands

Back up without deleting anything:

```bash
python3 harbor2oss_sync.py --no-prune
```

Process one repository:

```bash
python3 harbor2oss_sync.py --repo monkeys-studio
```

Keep a different number of tags:

```bash
python3 harbor2oss_sync.py --keep-tags 20
```

Pass arguments in Zeabur without changing the image:

```text
JOB_ARGS=--repo monkeys-studio --dry-run
```

## Notes

- Do not commit real credentials. Configure them in Zeabur environment
  variables.
- Harbor immutable tags cannot be deleted. The script logs those failures and
  continues.
- Harbor quota dropped only after deleting the artifact, not after deleting only
  the tag. This script therefore uses artifact deletion when the entire artifact
  is outside the retention window.
- Keep container concurrency low. The script is intentionally sequential because
  temporary storage in Zeabur containers is limited.
