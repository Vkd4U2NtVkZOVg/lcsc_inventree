# LCSC → InvenTree Web 服务
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LCSC_CONFIG_DIR=/opt/lcsc2inventree/config \
    LCSC_CACHE_DIR=/var/lib/lcsc2inventree/cache \
    INVENTREE_BACKUP_STORAGE=/backup

WORKDIR /opt/lcsc2inventree

# 先装依赖层（利用缓存）
COPY pyproject.toml uv.lock README.md ./
COPY lcsc2inv ./lcsc2inv
COPY config ./config

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .[web]

# 非 root 运行。
# 群晖等宿主机的 docker.sock 是 root:root 660（组 GID 0），
# 让 appuser 加入 supplementary group 0（容器内 root 组，非宿主机 root）才能访问 socket
RUN useradd --create-home --uid 10001 --groups 0 appuser \
    && mkdir -p /var/lib/lcsc2inventree/cache /backup \
    && chown -R appuser:appuser /var/lib/lcsc2inventree /opt/lcsc2inventree /backup \
    && mkdir -p /home/appuser/.cache \
    && cp -a /ms-playwright /home/appuser/.cache/ms-playwright \
    && chown -R appuser:appuser /home/appuser/.cache
USER appuser

EXPOSE 8080

# 单 worker：InvenTree SDK 非线程安全，LCSC 有 1 req/s 限速
# TLS：挂载 /certs/nas.crt + /certs/nas.key 时自动启用 HTTPS，否则退回 HTTP
CMD ["sh", "-c", "if [ -f /certs/nas.crt ] && [ -f /certs/nas.key ]; then \
      exec gunicorn -w 1 -t 300 -b 0.0.0.0:8080 \
        --certfile=/certs/nas.crt --keyfile=/certs/nas.key lcsc2inv.web:app; \
    else \
      exec gunicorn -w 1 -t 300 -b 0.0.0.0:8080 lcsc2inv.web:app; \
    fi"]