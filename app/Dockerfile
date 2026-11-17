# syntax=docker/dockerfile:1
# 三班倒打卡系统 —— 通用镜像构建（不含任何客户数据或客户部署配置）。

FROM python:3.12-slim AS api-build
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY app/api/pyproject.toml /build/api/pyproject.toml
RUN python -c 'import tomllib; data=tomllib.load(open("/build/api/pyproject.toml","rb")); print("\n".join(data["project"]["dependencies"]))' > /tmp/requirements.txt \
    && pip install --no-cache-dir --prefer-binary -r /tmp/requirements.txt
COPY app/api /build/api

FROM node:22-slim AS web-build
WORKDIR /build/web
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable && corepack prepare pnpm@11.5.0 --activate
COPY app/web/package.json app/web/pnpm-lock.yaml app/web/pnpm-workspace.yaml ./
RUN mkdir -p apps/admin apps/h5 packages/api-client
COPY app/web/apps/admin/package.json apps/admin/package.json
COPY app/web/apps/h5/package.json apps/h5/package.json
COPY app/web/packages/api-client/package.json packages/api-client/package.json
RUN pnpm install --frozen-lockfile
COPY app/web /build/web
RUN pnpm -r build

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=api-build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=api-build /usr/local/bin /usr/local/bin
COPY app/api /app/api
COPY --from=web-build /build/web/apps/admin/dist /app/web/apps/admin/dist
COPY --from=web-build /build/web/apps/h5/dist /app/web/apps/h5/dist

# 通用默认值；正式部署通过环境变量覆盖。测试身份默认关闭。
ENV DATABASE_URL=postgresql://app:app@db:5432/app \
    STATIC_ADMIN=/app/web/apps/admin/dist \
    STATIC_H5=/app/web/apps/h5/dist \
    FDC_TEST_MODE=0 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/api
WORKDIR /app/api
EXPOSE 3000
CMD ["bash", "-c", "python -m app.migrate --skip-online-index && uvicorn app.main:app --host 0.0.0.0 --port 3000"]
