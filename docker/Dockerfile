# 社区智慧养老服务与健康监测平台 · 应用镜像
# 构建：docker build -f docker/Dockerfile -t eldercare-platform:1.0.0 .
FROM python:3.12-slim

LABEL maintainer="eldercare-platform" \
      description="社区智慧养老服务与健康监测平台（FastAPI + Jinja2 + SQLite/PostgreSQL）"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖：tzdata 保证东八区时间；build-essential 供编译依赖使用（安装后清理）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "psycopg[binary]==3.2.3"

# 业务代码与静态资源（vendor 已本地化，运行期不访问外网）
COPY app ./app
COPY scripts ./scripts
COPY docs ./docs
COPY README.md ./
COPY pyproject.toml pytest.ini ./

# 运行时目录（数据库 / 上传 / 备份 / 临时文件）
RUN mkdir -p /app/data/uploads /app/data/backups /app/data/tmp

ENV DATABASE_URL=sqlite:////app/data/app.db \
    UPLOAD_DIR=/app/data/uploads \
    BACKUP_DIR=/app/data/backups \
    DATA_DIR=/app/data \
    TMPDIR=/app/data/tmp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 首次启动自动建表 + 填充演示数据（已初始化则幂等跳过），随后启动服务
CMD ["sh", "-c", "python -m scripts.seed && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
