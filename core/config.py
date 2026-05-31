from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "inspection_workflow"
    server_port: int = 8100
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+psycopg://inspection:inspection@localhost:5432/inspection_workflow"

    # PTS API
    pts_graphql_url: str = "http://api.in.chaitin.net/pts/query"
    pts_api_token: str = ""
    pts_rate_limit: float = 4.0  # max requests per second
    pts_session_cookie: str = ""  # PTS 网页端 session cookie (c=xxx)，用于文件上传

    # DingTalk AITable - 日常增值服务进展 (sync + monitor)
    dt_aitable_base_id: str = ""
    dt_aitable_table_id: str = ""
    dt_poll_interval: int = 7200  # seconds (2小时)
    dt_poll_enabled: bool = True

    # DingTalk AITable - 客户巡检派单 (monitor only)
    dt_dispatch_base_id: str = ""  # YndMj49yWjPL7gq7TwPpArYyJ3pmz5aA
    dt_dispatch_table_id: str = ""  # UWdhzcr

    # Trigger controls
    auto_dispatch_enabled: bool = False  # 自动触发云集派单（默认关闭）
    auto_email_enabled: bool = False  # 自动触发邮件发送（默认关闭）

    # External Services
    yunji_session_cookie: str = ""  # yunji_session_id=xxx; go-server-token=yyy
    inspection_email_smtp_host: str = "smtpdm.aliyun.com"
    inspection_email_smtp_port: int = 465
    inspection_email_sender: str = "inspection@product-support.chaitin.com"
    inspection_email_password: str = ""
    ai_api_key: str = ""

    # Email tool (Streamlit)
    email_tool_port: int = 8502  # Streamlit 邮件工具端口

    # Scheduler
    sync_cron: str = "0 16 * * *"
    email_probe_cron: str = "0 */2 * * *"  # 每2小时探测一次待发邮件数据
    closure_check_cron: str = "0 10 * * *"  # 每天10点检测未闭环工单
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    # Email pre-analysis
    email_pre_analysis_enabled: bool = True  # 启用邮件预分析定时任务
    email_pre_analysis_cron: str = "0 9 * * *"  # 每天9点运行一次预分析

    # DingTalk Notification
    dingtalk_webhook_url: str = ""  # 钉钉机器人 webhook URL
    dingtalk_secret: str = ""  # 钉钉机器人加签密钥
    dingtalk_holiday_mute: bool = True  # 法定节假日及非工作日不发送钉钉通知

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
