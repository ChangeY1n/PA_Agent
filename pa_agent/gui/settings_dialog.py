"""Settings dialog for PA Agent — edits all Settings fields via a form."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices

from pa_agent.config.settings import AIProviderSettings, Settings, save_settings

_API_KEY_HELP_URL = "https://my.feishu.cn/wiki/CUV1wUKWxiQGhekQdRvcZQQ2ncf"
_AGENT_TUTORIAL_URL = (
    "https://my.feishu.cn/wiki/BEdFwGJhaiATbukuD2HccSXCnrb?from=from_copylink"
)


class SettingsDialog(QDialog):
    """Modal dialog that exposes all Settings fields as editable form widgets."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self._settings = settings
        # 方案库工作副本：新增/删除/切换只动副本，取消对话框不污染运行中设置
        self._profiles: dict[str, AIProviderSettings] = {
            name: profile.model_copy()
            for name, profile in settings.provider_profiles.items()
        }
        self._setup_ui()
        self._load_values()

    def _setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        form_layout = QVBoxLayout(container)
        form_layout.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(container)
        root_layout.addWidget(scroll)

        provider_group = QGroupBox("AI 提供商")
        provider_form = QFormLayout(provider_group)

        # ── 多 API 配置方案：保存多套提供商配置，选择当前使用哪一套 ──────────
        profile_row = QHBoxLayout()
        self._profile_combo = QComboBox()
        self._profile_combo.setToolTip(
            "选择要使用的 API 配置方案；选中后其配置载入下方表单，点击「保存」生效并持久化（重启后默认使用所选方案）"
        )
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        profile_row.addWidget(self._profile_combo, 1)
        self._profile_save_as_btn = QPushButton("另存为新方案")
        self._profile_save_as_btn.setToolTip(
            "把当前表单中的配置保存为一个新命名方案（不切换已有方案的内容）"
        )
        self._profile_save_as_btn.clicked.connect(self._on_profile_save_as)
        profile_row.addWidget(self._profile_save_as_btn)
        self._profile_delete_btn = QPushButton("删除")
        self._profile_delete_btn.setToolTip("删除所选方案（至少保留一个）")
        self._profile_delete_btn.clicked.connect(self._on_profile_delete)
        profile_row.addWidget(self._profile_delete_btn)
        provider_form.addRow("配置方案:", profile_row)
        self._repopulate_profile_combo(
            str(getattr(self._settings, "active_provider_name", "") or "")
        )

        self._model_edit = QLineEdit()
        provider_form.addRow("模型 (model):", self._model_edit)

        self._base_url_edit = QLineEdit()
        provider_form.addRow("Base URL:", self._base_url_edit)

        api_key_row = QHBoxLayout()
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("输入 API Key")
        api_key_row.addWidget(self._api_key_edit)
        self._show_key_btn = QPushButton("显示")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.setFixedWidth(52)
        self._show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        api_key_row.addWidget(self._show_key_btn)
        provider_form.addRow("API Key:", api_key_row)

        self._thinking_check = QCheckBox("启用 Thinking")
        provider_form.addRow("Thinking:", self._thinking_check)

        self._reasoning_effort_combo = QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high", "max"])
        provider_form.addRow("Reasoning Effort:", self._reasoning_effort_combo)

        self._context_window_spin = QSpinBox()
        self._context_window_spin.setRange(1_000, 2_000_000)
        self._context_window_spin.setSingleStep(1_000)
        provider_form.addRow("Context Window:", self._context_window_spin)

        self._api_key_help_btn = QPushButton("点击获取模型API KEY")
        self._api_key_help_btn.setToolTip(_API_KEY_HELP_URL)
        self._api_key_help_btn.clicked.connect(self._open_api_key_help_url)
        provider_form.addRow("", self._api_key_help_btn)

        self._agent_tutorial_btn = QPushButton("智能体使用教程及问题解决方法")
        self._agent_tutorial_btn.setToolTip(_AGENT_TUTORIAL_URL)
        self._agent_tutorial_btn.clicked.connect(self._open_agent_tutorial_url)
        provider_form.addRow("", self._agent_tutorial_btn)

        form_layout.addWidget(provider_group)

        general_group = QGroupBox("通用设置")
        general_form = QFormLayout(general_group)

        self._analysis_bar_count_spin = QSpinBox()
        self._analysis_bar_count_spin.setRange(2, 5_000)
        self._analysis_bar_count_spin.setToolTip(
            "提交 AI 分析时使用的已收盘 K 线根数（不含当前未收盘 K 线）。"
            "图表实时刷新也会按此数量拉取显示。"
        )
        general_form.addRow("用于分析的 K 线数量:", self._analysis_bar_count_spin)

        self._refresh_interval_spin = QSpinBox()
        self._refresh_interval_spin.setRange(100, 10_000)
        self._refresh_interval_spin.setSuffix(" ms")
        general_form.addRow("刷新间隔:", self._refresh_interval_spin)

        self._auto_resume_chart_check = QCheckBox("分析完成后自动恢复「图表实时更新」")
        self._auto_resume_chart_check.setToolTip(
            "提交分析时图表会暂停刷新并冻结为已收盘 K 线；"
            "勾选后，分析结束（成功或校验失败但流程已跑完）将自动恢复实时刷新，"
            "并重新显示最右侧未收盘空心 K 线。演示模式不受影响。"
        )
        general_form.addRow("图表:", self._auto_resume_chart_check)

        self._keep_analysis_check = QCheckBox("有新K线收盘时自动开始新一轮分析")
        self._keep_analysis_check.setToolTip(
            "勾选后，每当有新的K线收盘时自动触发分析（与主界面「持续跟踪分析」勾选框同步）"
        )
        general_form.addRow("持续跟踪分析:", self._keep_analysis_check)

        self._context_warning_spin = QSpinBox()
        self._context_warning_spin.setRange(1, 100)
        self._context_warning_spin.setSuffix(" %")
        general_form.addRow("上下文警告阈值:", self._context_warning_spin)

        self._stream_font_spin = QSpinBox()
        self._stream_font_spin.setRange(8, 28)
        self._stream_font_spin.setSuffix(" pt")
        self._stream_font_spin.setToolTip(
            "「实时」标签页中思考过程/撰写回答大文本框，以及下方追问输入框的字体大小"
        )
        general_form.addRow("实时窗口字号:", self._stream_font_spin)

        self._chart_seq_font_spin = QSpinBox()
        self._chart_seq_font_spin.setRange(6, 24)
        self._chart_seq_font_spin.setSuffix(" pt")
        self._chart_seq_font_spin.setToolTip("K 线图上 #1、#3… 序号标签的字体大小")
        general_form.addRow("图表K线序号字号:", self._chart_seq_font_spin)

        self._incremental_max_new_bars_spin = QSpinBox()
        self._incremental_max_new_bars_spin.setRange(0, 500)
        self._incremental_max_new_bars_spin.setSuffix(" 根")
        self._incremental_max_new_bars_spin.setToolTip(
            "同品种同周期下，若相对上一条成功记录只新增不超过该数量的已收盘K线，"
            "提交分析时走增量分析；设为 0 可关闭增量分析。"
        )
        general_form.addRow("增量分析最大新增K线:", self._incremental_max_new_bars_spin)

        self._decision_stance_combo = QComboBox()
        self._decision_stance_combo.addItem("保守", "conservative")
        self._decision_stance_combo.addItem("均衡（默认，比保守更愿意下单）", "balanced")
        self._decision_stance_combo.addItem("激进（比均衡更愿意下单）", "aggressive")
        self._decision_stance_combo.addItem(
            "极度激进（强制选方向与进场方式）",
            "extreme_aggressive",
        )
        self._decision_stance_combo.setToolTip(
            "仅影响阶段二交易决策倾向；保守与改版前一致。"
            "均衡、激进逐级提高下单意愿；极度激进在未触犯 §14 硬性禁止时"
            "必须给出具体做多/做空及限价/突破/市价方案。"
        )
        general_form.addRow("交易倾向:", self._decision_stance_combo)

        self._last_symbol_edit = QLineEdit()
        general_form.addRow("上次品种:", self._last_symbol_edit)

        self._last_timeframe_edit = QLineEdit()
        general_form.addRow("上次周期:", self._last_timeframe_edit)

        tdx_dir_row = QHBoxLayout()
        self._tdx_dir_edit = QLineEdit()
        self._tdx_dir_edit.setPlaceholderText("留空自动探测，如 D:\\app2\\tdx")
        self._tdx_dir_edit.setToolTip(
            "通达信安装目录（需含 T0002/blocknew/zxg.blk 自选股文件）。\n"
            "选择通达信 TDX 数据源后，股票下拉列表将包含自选股中的股票。\n"
            "留空时自动探测常见安装目录。"
        )
        tdx_dir_row.addWidget(self._tdx_dir_edit, 1)
        self._tdx_dir_browse_btn = QPushButton("浏览…")
        self._tdx_dir_browse_btn.setFixedWidth(72)
        self._tdx_dir_browse_btn.clicked.connect(self._on_browse_tdx_dir)
        tdx_dir_row.addWidget(self._tdx_dir_browse_btn)
        general_form.addRow("通达信目录:", tdx_dir_row)

        self._flow_auto_play_check = QCheckBox("决策树可视化生成后自动播放路径")
        general_form.addRow("决策树播放:", self._flow_auto_play_check)

        self._flow_play_seconds_spin = QSpinBox()
        self._flow_play_seconds_spin.setRange(3, 120)
        self._flow_play_seconds_spin.setSuffix(" 秒")
        general_form.addRow("播放时长:", self._flow_play_seconds_spin)

        self._flow_default_zoom_spin = QSpinBox()
        self._flow_default_zoom_spin.setRange(10, 9_999_999)
        self._flow_default_zoom_spin.setSuffix(" %")
        self._flow_default_zoom_spin.setToolTip(
            "相对「整图适配」视图：100% 与适配一致，50% 再缩小一半；"
            "可填任意更大百分比以放大（分析完成、播放路径、手动播放均用此比例）"
        )
        general_form.addRow("决策树可视化默认缩放:", self._flow_default_zoom_spin)

        self._flow_play_now_btn = QPushButton("播放决策树可视化")
        self._flow_play_now_btn.setToolTip(
            "使用当前已加载的决策路径重新播放动画（若尚未分析则无可播放内容）"
        )
        self._flow_play_now_btn.clicked.connect(self._on_play_decision_flow_now)
        general_form.addRow("", self._flow_play_now_btn)

        self._decision_flow_play_handler: Callable[[], None] | None = None

        form_layout.addWidget(general_group)

        # ── Decision log (CSV / Excel) ───────────────────────────────────────
        log_group = QGroupBox("交易决策日志（自动追加开仓决策）")
        log_form = QFormLayout(log_group)
        log_form.setLabelAlignment(log_form.labelAlignment())

        self._enable_csv_check = QCheckBox("记录到 CSV 文件")
        self._enable_csv_check.setToolTip(
            "勾选后，每次分析产生开仓决策（非「不下单」）时，会向 CSV 文件追加一行。"
        )
        log_form.addRow("CSV:", self._enable_csv_check)

        csv_path_row = QHBoxLayout()
        self._csv_path_edit = QLineEdit()
        self._csv_path_edit.setPlaceholderText("records/trading_decisions.csv")
        csv_path_row.addWidget(self._csv_path_edit, 1)
        self._csv_browse_btn = QPushButton("浏览…")
        self._csv_browse_btn.setFixedWidth(72)
        self._csv_browse_btn.clicked.connect(self._on_browse_csv_path)
        csv_path_row.addWidget(self._csv_browse_btn)
        log_form.addRow("CSV 路径:", csv_path_row)

        self._enable_excel_check = QCheckBox("记录到 Excel (.xlsx) 文件")
        self._enable_excel_check.setToolTip(
            "勾选后，每次分析产生开仓决策（非「不下单」）时，会向 Excel 工作簿追加一行。"
            "支持在 Excel 中直接打开查看。文件被 Excel 占用时会自动重试。"
        )
        log_form.addRow("Excel:", self._enable_excel_check)

        excel_path_row = QHBoxLayout()
        self._excel_path_edit = QLineEdit()
        self._excel_path_edit.setPlaceholderText("records/trading_decisions.xlsx")
        excel_path_row.addWidget(self._excel_path_edit, 1)
        self._excel_browse_btn = QPushButton("浏览…")
        self._excel_browse_btn.setFixedWidth(72)
        self._excel_browse_btn.clicked.connect(self._on_browse_excel_path)
        excel_path_row.addWidget(self._excel_browse_btn)
        log_form.addRow("Excel 路径:", excel_path_row)

        self._open_log_dir_btn = QPushButton("打开日志所在文件夹")
        self._open_log_dir_btn.setToolTip("在文件管理器中显示日志文件所在目录")
        self._open_log_dir_btn.clicked.connect(self._on_open_log_dir)
        log_form.addRow("", self._open_log_dir_btn)

        form_layout.addWidget(log_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

    def _load_values(self) -> None:
        p = self._settings.provider
        g = self._settings.general

        self._model_edit.setText(p.model)
        self._base_url_edit.setText(p.base_url)
        self._api_key_edit.setText(p.api_key)
        self._thinking_check.setChecked(p.thinking)

        idx = self._reasoning_effort_combo.findText(p.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)

        self._context_window_spin.setValue(p.context_window)
        self._analysis_bar_count_spin.setValue(g.analysis_bar_count)
        self._refresh_interval_spin.setValue(g.refresh_interval_ms)
        self._auto_resume_chart_check.setChecked(
            bool(getattr(g, "auto_resume_chart_after_analysis", False))
        )
        self._keep_analysis_check.setChecked(
            bool(getattr(g, "keep_analysis", False))
        )
        self._context_warning_spin.setValue(int(g.context_warning_threshold_pct))
        self._stream_font_spin.setValue(int(getattr(g, "stream_pane_font_pt", 11)))
        self._chart_seq_font_spin.setValue(int(getattr(g, "chart_seq_label_font_pt", 7)))
        self._incremental_max_new_bars_spin.setValue(
            int(getattr(g, "incremental_max_new_bars", 10))
        )
        stance = getattr(g, "decision_stance", "conservative")
        stance_idx = self._decision_stance_combo.findData(stance)
        if stance_idx >= 0:
            self._decision_stance_combo.setCurrentIndex(stance_idx)
        self._last_symbol_edit.setText(g.last_symbol)
        self._last_timeframe_edit.setText(g.last_timeframe)
        self._tdx_dir_edit.setText(str(getattr(g, "tdx_install_dir", "")))
        self._flow_auto_play_check.setChecked(
            getattr(g, "decision_flow_auto_play", False)
        )
        self._flow_play_seconds_spin.setValue(
            getattr(g, "decision_flow_play_seconds", 50)
        )
        self._flow_default_zoom_spin.setValue(
            int(getattr(g, "decision_flow_default_zoom_pct", 500))
        )
        self._enable_csv_check.setChecked(
            bool(getattr(g, "enable_decision_csv_logging", True))
        )
        self._csv_path_edit.setText(
            str(getattr(g, "decision_csv_path", "records/trading_decisions.csv"))
        )
        self._enable_excel_check.setChecked(
            bool(getattr(g, "enable_decision_excel_logging", True))
        )
        self._excel_path_edit.setText(
            str(getattr(g, "decision_excel_path", "records/trading_decisions.xlsx"))
        )

    # ── API 配置方案（Provider Profiles）─────────────────────────────────────

    def _repopulate_profile_combo(self, select_name: str) -> None:
        """重建方案下拉列表并选中 select_name（无信号副作用）."""
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for name in self._profiles:
            self._profile_combo.addItem(name, name)
        idx = self._profile_combo.findData(select_name)
        if idx < 0:
            idx = 0
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)
        self._profile_delete_btn.setEnabled(len(self._profiles) > 1)

    def _selected_profile_name(self) -> str:
        return str(self._profile_combo.currentData() or "")

    def _load_profile_into_form(self, profile: AIProviderSettings) -> None:
        self._model_edit.setText(profile.model)
        self._base_url_edit.setText(profile.base_url)
        self._api_key_edit.setText(profile.api_key)
        self._thinking_check.setChecked(bool(profile.thinking))
        idx = self._reasoning_effort_combo.findText(profile.reasoning_effort)
        if idx >= 0:
            self._reasoning_effort_combo.setCurrentIndex(idx)
        self._context_window_spin.setValue(int(profile.context_window))

    def _on_profile_selected(self, _index: int) -> None:
        """下拉切换方案：把该方案配置载入表单（点击「保存」后生效）."""
        profile = self._profiles.get(self._selected_profile_name())
        if profile is not None:
            self._load_profile_into_form(profile)

    def _form_provider_snapshot(self) -> AIProviderSettings:
        """按当前表单字段生成提供商配置（保留表单未暴露字段，如 max_completion_tokens）."""
        base = (
            self._profiles.get(self._selected_profile_name())
            or self._settings.provider
        )
        return base.model_copy(
            update={
                "model": self._model_edit.text().strip(),
                "base_url": self._base_url_edit.text().strip(),
                "api_key": self._api_key_edit.text(),
                "thinking": self._thinking_check.isChecked(),
                "reasoning_effort": self._reasoning_effort_combo.currentText(),
                "context_window": self._context_window_spin.value(),
            }
        )

    def _on_profile_save_as(self) -> None:
        name, ok = QInputDialog.getText(self, "另存为新方案", "方案名称:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self._profiles:
            answer = QMessageBox.question(
                self,
                "方案已存在",
                f"方案「{name}」已存在，是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._profiles[name] = self._form_provider_snapshot()
        self._repopulate_profile_combo(name)

    def _on_profile_delete(self) -> None:
        name = self._selected_profile_name()
        if not name or len(self._profiles) <= 1:
            return
        answer = QMessageBox.question(
            self,
            "删除方案",
            f"确定删除方案「{name}」？（点击「保存」后生效）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del self._profiles[name]
        self._repopulate_profile_combo(next(iter(self._profiles), ""))
        # 分隔线后表单切到删除后选中的方案，避免表单仍显示已删内容
        self._on_profile_selected(self._profile_combo.currentIndex())

    @staticmethod
    def _validate_provider_fields(model: str, base_url: str) -> str | None:
        """Return user-facing error text, or None if fields look consistent."""
        if model.startswith(("http://", "https://")) and not base_url.startswith(
            ("http://", "https://")
        ):
            return (
                "「模型」与「Base URL」似乎填反了：\n"
                "• 模型应填模型名，如 deepseek-v4-pro 或 claude-sonnet-4-6\n"
                "• Base URL 应填接口地址，如 https://api.deepseek.com"
            )
        if base_url.startswith(("http://", "https://")):
            return None
        if not base_url:
            return "请填写 Base URL（API 接口地址）。"
        return (
            f"Base URL 不是有效网址（当前：{base_url}）。\n"
            "DeepSeek 示例：https://api.deepseek.com\n"
            "PackyAPI 示例：https://www.packyapi.com/v1"
        )

    def _on_save(self) -> None:
        p = self._settings.provider
        g = self._settings.general

        model = self._model_edit.text().strip()
        base_url = self._base_url_edit.text().strip()
        field_err = self._validate_provider_fields(model, base_url)
        if field_err:
            QMessageBox.warning(self, "AI 提供商配置有误", field_err)
            return

        p.model = model
        p.base_url = base_url
        p.api_key = self._api_key_edit.text()
        p.thinking = self._thinking_check.isChecked()
        p.reasoning_effort = self._reasoning_effort_combo.currentText()  # type: ignore[assignment]
        p.context_window = self._context_window_spin.value()

        # ── 配置方案：所选方案与表单同步，并标记为当前使用（启动默认） ──────────
        profile_name = self._selected_profile_name()
        if profile_name:
            self._profiles[profile_name] = self._form_provider_snapshot()
            self._settings.provider_profiles = {
                name: profile.model_copy() for name, profile in self._profiles.items()
            }
            self._settings.active_provider_name = profile_name

        g.analysis_bar_count = self._analysis_bar_count_spin.value()
        g.refresh_interval_ms = self._refresh_interval_spin.value()
        g.auto_resume_chart_after_analysis = self._auto_resume_chart_check.isChecked()
        g.keep_analysis = self._keep_analysis_check.isChecked()
        g.context_warning_threshold_pct = float(self._context_warning_spin.value())
        g.stream_pane_font_pt = self._stream_font_spin.value()
        g.chart_seq_label_font_pt = self._chart_seq_font_spin.value()
        g.incremental_max_new_bars = self._incremental_max_new_bars_spin.value()
        g.decision_stance = self._decision_stance_combo.currentData()  # type: ignore[assignment]
        g.last_symbol = self._last_symbol_edit.text().strip()
        g.last_timeframe = self._last_timeframe_edit.text().strip()
        g.tdx_install_dir = self._tdx_dir_edit.text().strip()
        g.decision_flow_auto_play = self._flow_auto_play_check.isChecked()
        g.decision_flow_play_seconds = self._flow_play_seconds_spin.value()
        g.decision_flow_default_zoom_pct = self._flow_default_zoom_spin.value()
        g.enable_decision_csv_logging = self._enable_csv_check.isChecked()
        g.decision_csv_path = self._csv_path_edit.text().strip() or "records/trading_decisions.csv"
        g.enable_decision_excel_logging = self._enable_excel_check.isChecked()
        g.decision_excel_path = (
            self._excel_path_edit.text().strip() or "records/trading_decisions.xlsx"
        )

        # Propagate to live loggers so the next write uses the new path/state
        ctx = getattr(self.parent(), "_ctx", None) if self.parent() is not None else None
        if ctx is not None:
            csv_logger = getattr(ctx, "csv_logger", None)
            if csv_logger is not None and hasattr(csv_logger, "set_enabled"):
                csv_logger.set_enabled(g.enable_decision_csv_logging)
            if csv_logger is not None and hasattr(csv_logger, "set_csv_path"):
                csv_logger.set_csv_path(g.decision_csv_path)
            excel_logger = getattr(ctx, "excel_logger", None)
            if excel_logger is not None and hasattr(excel_logger, "set_enabled"):
                excel_logger.set_enabled(g.enable_decision_excel_logging)
            if excel_logger is not None and hasattr(excel_logger, "set_xlsx_path"):
                excel_logger.set_xlsx_path(g.decision_excel_path)

        # 通达信：目录变更后立即应用到当前数据源并刷新股票下拉列表（含自选股）
        parent = self.parent()
        if parent is not None and hasattr(parent, "_populate_symbol_combo_for_source"):
            ctx = getattr(parent, "_ctx", None)
            tdx_source = getattr(ctx, "data_source", None) if ctx is not None else None
            if (
                tdx_source is not None
                and hasattr(tdx_source, "set_tdx_dir")
                and hasattr(parent, "_current_data_source_kind")
                and parent._current_data_source_kind() == "tdx"
            ):
                tdx_source.set_tdx_dir(g.tdx_install_dir or None)
                parent._populate_symbol_combo_for_source()

        # 默认路径（pa_agent.config.paths.SETTINGS_JSON_PATH）在函数内解析，
        # 测试可通过 patch 该常量隔离真实配置文件。
        save_settings(self._settings)
        self.accept()

    def focus_api_key_field(self) -> None:
        """Focus the API Key field (e.g. when prompting on first launch)."""
        self._api_key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._api_key_edit.selectAll()

    def set_decision_flow_play_handler(self, handler: Callable[[], None] | None) -> None:
        """Register callback invoked when user clicks 播放决策树可视化."""
        self._decision_flow_play_handler = handler

    def _on_play_decision_flow_now(self) -> None:
        # Allow previewing playback without pressing “保存”:
        # sync relevant fields from widgets into the in-memory settings object.
        g = self._settings.general
        g.decision_flow_auto_play = self._flow_auto_play_check.isChecked()
        g.decision_flow_play_seconds = self._flow_play_seconds_spin.value()
        g.decision_flow_default_zoom_pct = self._flow_default_zoom_spin.value()

        if self._decision_flow_play_handler is not None:
            self._decision_flow_play_handler()

    def _open_api_key_help_url(self) -> None:
        QDesktopServices.openUrl(QUrl(_API_KEY_HELP_URL))

    # ── Decision log helpers ───────────────────────────────────────────────

    def _on_browse_csv_path(self) -> None:
        """Open a Save-As dialog to pick a CSV destination path."""
        current = self._csv_path_edit.text().strip() or "records/trading_decisions.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择 CSV 决策日志路径",
            current,
            "CSV 文件 (*.csv);;所有文件 (*.*)",
        )
        if path:
            self._csv_path_edit.setText(path)

    def _on_browse_excel_path(self) -> None:
        """Open a Save-As dialog to pick an Excel destination path."""
        current = self._excel_path_edit.text().strip() or "records/trading_decisions.xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择 Excel 决策日志路径",
            current,
            "Excel 工作簿 (*.xlsx);;所有文件 (*.*)",
        )
        if path:
            self._excel_path_edit.setText(path)

    def _on_browse_tdx_dir(self) -> None:
        """Open a directory picker for the TongDaXin install dir."""
        current = self._tdx_dir_edit.text().strip()
        if not current:
            from pa_agent.data.tdx_watchlist import find_tdx_install_dir

            detected = find_tdx_install_dir()
            current = str(detected) if detected is not None else ""
        path = QFileDialog.getExistingDirectory(
            self, "选择通达信安装目录（需含 T0002 子目录）", current
        )
        if path:
            self._tdx_dir_edit.setText(path)

    def _on_open_log_dir(self) -> None:
        """Reveal the directory of the configured log file in Explorer."""
        from pathlib import Path

        for raw in (
            self._csv_path_edit.text().strip(),
            self._excel_path_edit.text().strip(),
        ):
            if not raw:
                continue
            folder = Path(raw).expanduser().resolve().parent
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            return
        QMessageBox.information(self, "决策日志", "请先填写日志文件路径。")

    def _open_agent_tutorial_url(self) -> None:
        QDesktopServices.openUrl(QUrl(_AGENT_TUTORIAL_URL))

    def _toggle_api_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("隐藏")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("显示")
