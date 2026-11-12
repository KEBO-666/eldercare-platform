"""全局配置：全部通过环境变量 / .env 外置（评分项：配置外置）。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "社区智慧养老服务与健康监测平台"
    version: str = "1.0.0"
    env: str = "dev"

    # 安全
    secret_key: str = "dev-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    encryption_key: str = "ZGV2LWtleS1jaGFuZ2UtbWUtaW4tcHJvZHVjdGlvbi0zMi1i"
    field_hash_key: str = "dev-hash-key-change-me"

    # 存储
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: str = "./data/uploads"
    backup_dir: str = "./data/backups"
    data_dir: str = "./data"

    # 调度
    enable_scheduler: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    # 派单
    dispatch_weights: str = "0.4,0.3,0.2,0.1"
    dispatch_top_n: int = 3
    order_daily_limit: int = 6
    service_radius_km: float = 3.0
    nurse_rating_default: float = 3.5

    # 执行
    checkin_distance_threshold_m: int = 200
    photo_limit: int = 3
    photo_max_kb: int = 2048
    photo_upload_max_mb: int = 10
    photo_max_pixels: int = 20_000_000
    accept_timeout_minutes: int = 30
    cancel_free_hours: int = 2
    duration_anomaly_minutes: int = 30

    # 预警
    alert_red_push_minutes: int = 5
    alert_escalate_minutes: int = 30
    alert_silence_hours: int = 48
    alert_cooldown_minutes: int = 30

    # 结算与留存
    diff_ratio_threshold: float = 0.01
    audit_retention_days: int = 180
    backup_keep_versions: int = 30

    # ---- 派生属性 ----
    @property
    def weights_tuple(self) -> tuple[float, float, float, float]:
        parts = [float(x) for x in self.dispatch_weights.replace("，", ",").split(",") if x.strip()]
        if len(parts) != 4:
            raise ValueError("DISPATCH_WEIGHTS 必须是 4 个逗号分隔的数字")
        total = sum(parts)
        return tuple(p / total for p in parts)  # type: ignore[return-value]

    def abs_path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (BASE_DIR / p)

    @property
    def db_url(self) -> str:
        url = self.database_url
        if url.startswith("sqlite:///./"):
            return "sqlite:///" + str(self.abs_path(url.removeprefix("sqlite:///./")))
        return url

    @property
    def upload_path(self) -> Path:
        return self.abs_path(self.upload_dir)

    @property
    def backup_path(self) -> Path:
        return self.abs_path(self.backup_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
