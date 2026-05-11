# Harbor 到 OSS 备份

[English](README.md)

将 Harbor 项目中的带 tag 镜像备份到 OSS，保存为 Docker archive tar
文件；确认备份已存在于 OSS 后，可选择清理 Harbor 中的旧制品。

## 功能

- 列出一个 Harbor 项目下的所有仓库。
- 使用 `skopeo` 导出尚未备份的镜像 tag。
- 将镜像归档上传到 OSS，大文件自动使用分片上传。
- 对 Harbor 和 OSS 的临时网络失败进行指数退避重试。
- 每个仓库保留最新的 `KEEP_TAGS` 个 tag。
- 只有确认 tag 已备份到 OSS 后，才删除 Harbor 中的旧制品。
- 支持一次性脚本运行，也支持容器内 cron 定时运行。
- 默认输出英文日志，设置 `LOG_LANGUAGE=zh` 后输出中文日志。

## 保留策略

脚本会按 Harbor 的 `push_time` 对 tag 排序，并为每个仓库保留最新的
`KEEP_TAGS` 个 tag。

如果某个旧制品上的所有 tag 都已经超出保留窗口，脚本会删除该制品：

```text
DELETE /api/v2.0/projects/{project}/repositories/{repo}/artifacts/{digest}
```

如果某个制品同时包含需要保留和已经过期的 tag，脚本只删除过期 tag，并保留制品本身。

## 运行要求

- Python 3.12+
- `skopeo`
- 一个 Harbor 账号；需要能够列出仓库/制品，并在启用清理时删除旧 tag/制品。
- 一组具备 OSS 对象读写权限的阿里云 OSS 凭证。

安装 Python 依赖：

```bash
python3 -m pip install -r requirements.txt
```

## 配置

必需环境变量：

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

可选环境变量：

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

`NETWORK_RETRIES` 适用于 Harbor API 请求和 OSS 操作。重试会从
`RETRY_BASE_DELAY_SECONDS` 开始进行指数退避。

设置 `LOG_LANGUAGE=zh` 可以输出中文运行日志。默认值为英文。

## 用法

试运行：

```bash
python3 harbor2oss_sync.py --dry-run
```

只备份，不删除：

```bash
python3 harbor2oss_sync.py --no-prune
```

只处理 Harbor 仓库全名中包含指定字符串的仓库：

```bash
python3 harbor2oss_sync.py --repo my-service
```

调整每个仓库保留的 tag 数量：

```bash
python3 harbor2oss_sync.py --keep-tags 20
```

向容器任务传递脚本参数：

```text
JOB_ARGS=--repo my-service --no-prune
```

## 容器

构建镜像：

```bash
docker build -t harbor-to-oss-backup .
```

运行一次：

```bash
docker run --rm --env-file .env harbor-to-oss-backup python /app/harbor2oss_sync.py --no-prune
```

使用内置 cron 入口运行：

```bash
docker run --rm --env-file .env harbor-to-oss-backup
```

默认计划任务为：

```text
0 0 * * *
```

Docker 镜像默认使用 `TZ=Asia/Shanghai`。

## 安全提示

- 不要提交真实凭证。请将凭证保存在部署环境或本地 `.env` 文件中。
- 启用清理前，建议先使用 `--dry-run` 或 `--no-prune` 验证行为。
- Harbor 不可变 tag 无法删除；脚本会记录失败并继续执行。
- 对于同一个 Harbor 项目和 OSS 前缀，请一次只运行一个任务。
- 请确保容器有足够的临时磁盘空间来存放最大的镜像归档文件。
