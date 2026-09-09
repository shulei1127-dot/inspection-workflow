from functools import lru_cache

from pydantic import model_validator
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
    pts_session_cookie: str = ""  # PTS 网页端 session cookie (c=xxx)，用于 Playwright 文件上传（已废弃）
    pts_upload_url: str = ""  # PTS 内网文件上传端点，从 pts_graphql_url 自动推导，无需手动配置

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

    # Inspection closure V2 (disabled by default; legacy closure remains unchanged)
    inspection_closure_v2_enabled: bool = False
    inspection_closure_dry_run: bool = True
    inspection_closure_report_upload_enabled: bool = False
    inspection_closure_stage_advance_enabled: bool = False
    inspection_closure_aitable_writeback_enabled: bool = False
    inspection_closure_manual_notify_enabled: bool = False
    dt_dispatch_report_link_uploaded_field_id: str = ""
    inspection_closure_whitelist: str = ""  # 留空=全量处理；非空=仅处理白名单记录（应急限制开关）
    inspection_closure_default_assignee_id: str = "669723ae2f6e1a862a49ef16"
    inspection_closure_upload_max_retries: int = 3
    inspection_closure_stage_max_attempts: int = 10
    inspection_closure_retry_backoff_seconds: float = 2.0
    inspection_closure_retry_max_backoff_seconds: float = 30.0

    # Inspection info library (巡检信息库)
    inspection_library_enabled: bool = False  # 巡检信息库同步+回写总开关
    inspection_library_cron: str = "0 18 * * *"  # 每天18点同步信息库并回写缺失地址/邮箱

    # Scheduler
    sync_cron: str = "0 16 * * *"
    email_probe_cron: str = "0 */2 * * *"  # 每2小时探测一次待发邮件数据
    closure_check_cron: str = "0 20 * * *"  # 每天20点检测未闭环工单（基于邮件是否发送触发）
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    # Email pre-analysis
    email_pre_analysis_enabled: bool = True  # 启用邮件预分析定时任务
    email_pre_analysis_cron: str = "0 9 * * *"  # 每天9点运行一次预分析

    # DingTalk Notification
    dingtalk_webhook_url: str = ""  # 钉钉机器人 webhook URL
    dingtalk_secret: str = ""  # 钉钉机器人加签密钥
    dingtalk_holiday_mute: bool = True  # 法定节假日及非工作日不发送钉钉通知

    # Review (交付转售后审核)
    review_pipeline_enabled: bool = True  # 审核定时流水线总开关
    review_pipeline_cron: str = "0 16 * * *"  # 审核定时任务 cron
    review_real_execution_enabled: bool = True  # 真实执行开关（采集+审核+写入）
    review_writeback_enabled: bool = True  # 钉钉写入开关
    pts_review_api_token: str = ""  # 审核 PTS API Token（空则回退到 pts_api_token）
    pts_review_approval_api_key: str = ""  # 审核通过/拒绝专用 API Key
    pts_review_after_sale_filter_ids: str = ""  # 按售后 PTS 用户 ID 过滤（逗号分隔）
    review_license_autofill_enabled: bool = True  # 审核时按机器码关联 License 自动补全 License ID
    review_license_autofill_writeback: bool = True  # 自动补全后写回 PTS 产品表单

    # Review 钉钉数据表配置
    review_aitable_base_id: str = "o14dA3GK8g5LavPaT7dDQqoxV9ekBD76"
    review_aitable_main_table_id: str = "Igz9TVd"
    review_aitable_corp_id: str = "ding56395822e2c6d50035c2f4657eb6378f"

    # Sales confirm (销售巡检确认表单推送)
    sales_confirm_enabled: bool = False  # 总开关（默认关闭）
    sales_confirm_cron: str = "0 9 * * *"  # 每天9点推送
    sales_confirm_dry_run: bool = True  # 是否仅扫描不发送（默认开启，避免误发）

    # Visit (交付转售后回访闭环)
    visit_pipeline_enabled: bool = False  # 回访流水线总开关（默认关闭）
    visit_pipeline_cron: str = "0 17 * * *"  # 回访定时任务 cron
    visit_real_execution_enabled: bool = False  # 真实执行开关（默认关闭）
    visit_execution_mode: str = "direct_http"  # 执行模式: direct_http | browser_profile
    visit_auto_retry_enabled: bool = True  # 失败自动重试
    visit_max_retries: int = 3  # 最大重试次数
    pts_visit_api_token: str = ""  # 回访专用 PTS Token（空则回退到 pts_review_api_token → pts_api_token）
    visit_default_satisfaction: int = 5  # 默认满意度评分 (1-5)

    # Daily digest (日报汇总通知)
    daily_digest_enabled: bool = True  # 日报总开关
    daily_digest_cron: str = "30 17 * * 1-5"  # 工作日17:30发送
    visit_default_note: str = "自动回访完成"  # 默认回访备注
    visit_writeback_enabled: bool = False  # 钉钉写入开关（默认关闭）
    visit_writeback_aitable_base_id: str = ""  # 回访写入的 AITable base ID
    visit_writeback_aitable_table_id: str = ""  # 回访写入的 AITable table ID
    visit_writeback_link_field_id: str = ""  # 回访链接字段 ID

    # Daily Change Summary (知识库变更)
    daily_change_summary_enabled: bool = False  # 每日变更摘要定时任务（默认关闭）
    daily_change_summary_cron: str = "0 18 * * *"  # 每天18点收集并推送变更摘要
    daily_change_summary_repo_path: str = "/app"  # Git 仓库路径（容器内）
    daily_change_summary_webhook_url: str = ""  # 变更摘要钉钉机器人 webhook（空则用默认）

    # OIDC auth (公司内部统一认证 auth.chaitin.net)
    oidc_enabled: bool = False  # 是否启用访问认证（默认关闭，不影响线上）
    oidc_base_url: str = "https://auth.chaitin.net"  # IdP 根地址
    oidc_client_id: str = ""  # 应用客户端 ID（工单申请获得）
    oidc_client_secret: str = ""  # 客户端密钥（只放服务器 env，不入 Git）
    oidc_redirect_uri: str = ""  # 回调地址，须与注册完全一致
    oidc_scope: str = "profile"  # 运维注册的 client 仅允许 scope=profile（不允许 openid）
    oidc_cookie_secret: str = ""  # 会话 cookie 签名密钥（空则回退 client_secret）
    oidc_session_ttl_hours: int = 8  # 登录会话有效期（小时）
    oidc_state_ttl_seconds: int = 600  # OAuth state 有效期（秒）

    # Agent Hub (support-ai 纳管平台快照上报，默认关闭)
    agent_hub_enabled: bool = False  # 总开关（默认关闭，不影响线上）
    agent_hub_api_base: str = "https://support-ai.in.chaitin.net/agent-hub/api/v1"
    agent_hub_token: str = ""  # 平台签发的团队专属 Bearer Token，不入 Git
    agent_hub_token_file: str = ""  # root-only 密钥文件路径（容器 /run/secrets/agent_hub_token），与 TOKEN 二选一
    agent_hub_dashboard_url: str = ""  # 可选：覆盖 manifest 中的业务看板地址
    agent_hub_agent_key: str = "support.inspection-workflow"
    agent_hub_interval_minutes: int = 30  # 快照上报间隔（分钟）
    agent_hub_run_on_startup: bool = True  # 应用启动后先执行一次
    agent_hub_timeout_seconds: float = 20.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _derive_pts_upload_url(self) -> "Settings":
        """从 pts_graphql_url 自动推导 PTS 文件上传端点。

        生产环境 PTS_GRAPHQL_URL=http://10.9.255.197/pts/query
        推导为 http://10.9.255.197/pts/api/upload
        如果 .env 中显式配置了 PTS_UPLOAD_URL 则使用配置值。
        """
        if not self.pts_upload_url and self.pts_graphql_url:
            base = self.pts_graphql_url.rstrip("/")
            if base.endswith("/query"):
                self.pts_upload_url = base[:-6] + "/api/upload"
            else:
                self.pts_upload_url = base.replace("/query", "/api/upload")
        return self

    @model_validator(mode="after")
    def _check_oidc_config(self) -> "Settings":
        """启用认证时校验必需配置，缺项直接启动失败以便尽早发现。"""
        if not self.oidc_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("OIDC_CLIENT_ID", self.oidc_client_id),
                ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
                ("OIDC_REDIRECT_URI", self.oidc_redirect_uri),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"OIDC 认证已启用但缺少配置: {', '.join(missing)}")
        return self

    @property
    def oidc_authorize_url(self) -> str:
        return self.oidc_base_url.rstrip("/") + "/oauth2/auth"

    @property
    def oidc_token_url(self) -> str:
        return self.oidc_base_url.rstrip("/") + "/oauth2/token"

    @property
    def oidc_userinfo_url(self) -> str:
        return self.oidc_base_url.rstrip("/") + "/userinfo"

    def oidc_cookie_signing_secret(self) -> str:
        """Cookie 签名密钥：优先显式配置，其次回退 client_secret。"""
        return (self.oidc_cookie_secret or self.oidc_client_secret or "").strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
