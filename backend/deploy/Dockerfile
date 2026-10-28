# syntax=docker/dockerfile:1.7
# Luz 后端生产镜像（references/operations.md §2）。构建上下文为 backend/：
#   docker build -f deploy/Dockerfile --build-arg APP_VERSION=$(git describe --tags --always) -t luz-api:dev .
# 同一镜像运行 api（默认 CMD）、worker / scheduler（compose 覆盖 command）与 migrate（alembic）。
# 原则：多阶段构建、基础镜像按 digest 固定（Dependabot docker 生态更新）、非 root、exec 形式 CMD、镜像内无任何秘密。

# 基础镜像：tag 便于阅读，digest 才是真正的固定。上线前执行
#   docker buildx imagetools inspect python:3.12-slim-bookworm --format '{{json .Manifest.Digest}}'
# 并把下面的 sha256 替换为实际值（两个阶段必须同一 digest，保证 venv 与解释器 ABI 一致）。
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:0000000000000000000000000000000000000000000000000000000000000000

# ------------------------------------------------------------------ build：只在这一层装依赖
FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /w
# 生产应提供 requirements.lock（pip-compile --generate-hashes 或 uv pip compile --generate-hashes 产物）；
# REQUIRE_LOCK=1 时没有锁文件直接失败。requirements.lock* 的通配让锁文件缺失时 COPY 不报错。
ARG REQUIRE_LOCK=0
COPY requirements.txt requirements.lock* ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /venv \
 && if [ -f requirements.lock ]; then \
        /venv/bin/pip install --require-hashes --no-deps -r requirements.lock; \
    elif [ "$REQUIRE_LOCK" = "1" ]; then \
        echo "requirements.lock missing (REQUIRE_LOCK=1)" >&2; exit 1; \
    else \
        echo "WARNING: building without hash-locked requirements" >&2; \
        /venv/bin/pip install -r requirements.txt; \
    fi
# 运行时不需要 pip：减少攻击面与扫描噪声
RUN /venv/bin/python -m pip uninstall -y pip setuptools wheel

# ------------------------------------------------------------------ runtime：只含解释器、venv 与代码
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH=/venv/bin:$PATH \
    # uvicorn 从环境变量读取可信代理；compose 设为 nginx 固定地址。默认只信任本机，安全兜底
    FORWARDED_ALLOW_IPS=127.0.0.1

RUN groupadd --system --gid 10001 luz \
 && useradd --system --uid 10001 --gid luz --home-dir /srv --no-create-home --shell /usr/sbin/nologin luz

COPY --from=build /venv /venv
WORKDIR /srv/backend
# 代码属主 root、只读给 luz：运行时无法改写自身代码（配合 compose read_only）
COPY --chown=root:root . .

# 把框架与每个模块的 code/ 写进 .pth，worker/scheduler/alembic 与 app 使用同一套 import 路径（与 conftest.py 一致），
# 新增模块无需改 Dockerfile
RUN set -e; \
    site=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'); \
    { echo /srv/backend; ls -d /srv/backend/*/code; } > "$site/luz-modules.pth"; \
    python -m compileall -q /srv/backend

ARG APP_VERSION=dev
ARG VCS_REF=unknown
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.title="luz-api" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/<org>/<repo>"

USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM

# 镜像自带的是存活检查（/healthz，不查依赖，避免数据库抖动导致全部容器被判死）；
# compose 中 api 覆盖为 /readyz（就绪），用于 depends_on 与滚动发布切流。
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# 每个容器一个 uvicorn 进程（--workers 1），靠增加副本扩容；/metrics 计数为进程内，一容器一进程才能按实例汇总。
# --timeout-graceful-shutdown 20 < compose stop_grace_period 30s；--timeout-keep-alive 75 > nginx upstream keepalive_timeout 60s。
# --proxy-headers 只信任 FORWARDED_ALLOW_IPS；访问日志由 nginx JSON 日志负责，应用日志走 stdout JSON。
CMD ["uvicorn", "app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--timeout-graceful-shutdown", "20", \
     "--timeout-keep-alive", "75", \
     "--backlog", "2048", \
     "--no-server-header", \
     "--no-access-log"]
