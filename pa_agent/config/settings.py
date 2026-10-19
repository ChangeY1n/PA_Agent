"""Pydantic settings models for PA Agent."""
from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal["mt5", "tradingview", "akshare", "tdx"]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""
    model_config = ConfigDict(extra="ignore")

    model: str = "claude-sonnet-4-6"
    base_url: str = "https://www.packyapi.com/v1"
    api_key: str = ""
    api_key_encrypted: str = ""
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "max"
    context_window: int = 2_000_000
    #: Per-call completion ceiling for the OpenAI-style API. Caps reasoning+content.
    max_completion_tokens: int = 16384


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""
    model_config = ConfigDict(extra="ignore")

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    experience_max_entries: int = Field(default=3, ge=0, le=10)
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True
    #: Per-file character cap for Stage 2 strategy files. 0 disables the cap.
    #: Default 4000 keeps the file's "rules" head section (first ~3 KB) and
    #: trims the verbose examples tail, saving ~30% of Stage 2 prompt size.
    stage2_strategy_file_max_chars: int = Field(default=4000, ge=0, le=80000)


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""
    model_config = ConfigDict(extra="ignore")

    normalization_mode: NormalizationMode = "strict"
    trace_semantic_checks: bool = True
    strict_bar_by_bar_features: bool = True
    #: Do not inject stub gate_trace on truncated Stage 1 JSON.
    disable_truncation_repair: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""
    model_config = ConfigDict(extra="ignore")

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 80.0
    last_data_source: DataSourceKind = "mt5"
    #: TradingView 交易所；空字符串 =（自动）依次探测预设列表
    last_tradingview_exchange: str = ""
    #: 通达信安装目录（读取自选股列表用）；空字符串 = 自动探测常见目录
    tdx_install_dir: str = ""
    last_symbol: str = "XAUUSDm"
    last_timeframe: str = "15m"
    #: 收藏的品种（品种框旁 ★ 按钮切换；置顶显示在品种下拉列表）
    favorite_symbols: list[str] = Field(default_factory=list)
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=500, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=7, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 是否启用交易决策 CSV 日志记录
    enable_decision_csv_logging: bool = True
    #: 交易决策 CSV 文件路径
    decision_csv_path: str = "records/trading_decisions.csv"
    #: 是否启用交易决策 Excel (.xlsx) 日志记录
    enable_decision_excel_logging: bool = True
    #: 交易决策 Excel 文件路径
    decision_excel_path: str = "records/trading_decisions.xlsx"

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        if v == "yfinance":
            return "mt5"
        if v in ("adata", "a_share"):
            return "akshare"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v

    @field_validator("favorite_symbols", mode="before")
    @classmethod
    def _clean_favorite_symbols(cls, v: object) -> object:
        """strip、去空、去重保序."""
        if not isinstance(v, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for item in v:
            sym = str(item).strip()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
        return out


class BatchScreenSettings(BaseModel):
    """批量自选筛选窗口（工具菜单 → 批量自选筛选）的运行参数与初筛提示词。

    不进全局设置对话框——参数与提示词都在批量窗口内编辑，「开始」时保存。
    """
    model_config = ConfigDict(extra="ignore")

    workers: int = Field(default=10, ge=1, le=30)
    #: light=先轻量初筛再深析；full=全部直接完整两阶段
    mode: Literal["light", "full"] = "light"
    #: 只做多：初筛方向为空头的直接淘汰（A股无便利做空）
    long_only: bool = True
    #: 仅初筛不深析（校准/省钱巡视）
    light_only: bool = False
    #: 双初筛共识（Brooks 主筛 + 复核官并行，双通过才深析；token ×2 但更严）
    dual_screen: bool = True
    #: 初筛用 K 线根数（默认 80 = 前 60 根摘要 + 近 20 根明细 CSV）
    light_bars: int = Field(default=80, ge=20, le=200)
    #: 初筛 conviction 门槛（0=关；低于门槛不深析，省 token）
    min_conviction: int = Field(default=0, ge=0, le=100)
    #: 初筛 system 提示词覆盖（空 = light_screen 内置默认）
    light_system_prompt: str = ""
    #: 初筛 user 消息模板覆盖（空 = 内置默认；占位符见 LIGHT_SCREEN_USER_TEMPLATE）
    light_user_template: str = ""


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""
    model_config = ConfigDict(extra="ignore")

    #: 当前生效的 AI 提供商配置（切换方案 = 把所选方案字段写入此处）
    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    #: 命名的 AI 提供商方案库（名称 → 完整提供商配置）
    provider_profiles: dict[str, AIProviderSettings] = Field(default_factory=dict)
    #: 当前使用（也是启动默认）的方案名
    active_provider_name: str = ""
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    batch: BatchScreenSettings = Field(default_factory=BatchScreenSettings)

    @model_validator(mode="after")
    def _ensure_provider_profiles(self) -> "Settings":
        """维护不变式：方案库非空且 active_provider_name 指向存在的方案."""
        if not self.provider_profiles:
            self.provider_profiles = {"默认": self.provider.model_copy()}
            self.active_provider_name = "默认"
        elif self.active_provider_name not in self.provider_profiles:
            self.active_provider_name = next(iter(self.provider_profiles))
        return self


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_settings(path: Path | None = None) -> "Settings":
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    # Migrate legacy encrypted key: drop it, api_key already in provider dict
    raw.setdefault("provider", {}).setdefault("api_key", "")

    return Settings.model_validate(raw)


def save_settings(settings: "Settings", path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.model_dump()

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
