"""批量自选筛选窗口：CLI batch_watchlist_screen.py 的 GUI 版。

- 配置区：线程数 / 模式 / 只做多 / 仅初筛 / 初筛K线数 / conviction 门槛
- 初筛提示词编辑（system + user 模板，持久化在 settings.batch）
- 实时结果表：每只股票完成即追加一行；双击切到该品种（深析全文在
  records/pending，主窗口切过去即可查看/增量分析）
- 共享 CancelToken 停止：在飞任务经 analyze_one 内部中止，未提交的不再提交，
  已跑结果照常落盘 summary.json

窗口为 modeless（show()），MainWindow 持引用防 GC；关闭时若在运行会询问。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pa_agent.gui.theme import tokens as T

logger = logging.getLogger(__name__)

# 运行中的后台线程强引用集合：对话框可能先于线程死亡（关闭即 GC），
# 若线程只剩对话框里的引用，包装器会在 QThread 运行中被销毁 →
# access violation。线程自身 finished 时从这里移除并 deleteLater。
_ACTIVE_THREADS: set[QThread] = set()


def _retire_thread(self: QThread) -> None:  # noqa: ANN001 - 绑定到线程自身
    _ACTIVE_THREADS.discard(self)
    self.deleteLater()

_TABLE_COLUMNS = ["代码", "状态", "初筛", "conv", "订单", "入场", "止损", "目标", "信心", "摘要"]

_STATUS_FOREGROUND = {
    "opportunity": T.SUCCESS,
    "no_order": T.FG_2,
    "screened_out": T.FG_3,
    "light_pass": T.INFO,
    "limit_skip": T.WARNING,
    "failed": T.DANGER,
}

_STATUS_LABELS = {
    "opportunity": "★机会",
    "no_order": "—观望",
    "screened_out": "·淘汰",
    "light_pass": "+入围",
    "limit_skip": "■停板",
    "failed": "✗失败",
}


class _WatchlistProbeWorker(QThread):
    """后台读自选列表（watchlist_symbols() 是阻塞调用，不能在 GUI 线程做）。"""

    ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _ACTIVE_THREADS.add(self)
        self.finished.connect(lambda: _retire_thread(self))

    def run(self) -> None:  # pragma: no cover - IO 线程
        from pa_agent.data.factory import create_data_source

        probe = create_data_source("tdx")
        try:
            symbols = list(probe.watchlist_symbols())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        finally:
            try:
                probe.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.ready.emit(symbols)


class BatchScreenWorker(QThread):
    """跑一轮批量筛选：读自选 → 线程池 analyze_one → 汇总写 summary.json。

    有界提交（在飞 ≤ 2×workers）：取消令牌置位后不再提交剩余股票，
    已在飞的任务经 analyze_one 内部 abort，已完成的照常计入结果。
    """

    result_ready = pyqtSignal(object)          # 单只 result dict
    progress = pyqtSignal(int, int, str)        # done, total, 行摘要
    run_finished = pyqtSignal(dict)             # 汇总 + out_dir/cancelled
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        symbols: list[str],
        *,
        timeframe: str,
        bar_count: int,
        workers: int,
        mode: str,
        light_only: bool,
        light_bars: int,
        long_only: bool,
        min_conviction: int,
        dual_screen: bool = True,
        use_local_gate: bool = True,
        system_prompt: str = "",
        user_template: str = "",
        output_dir: str = "batch_analysis_results",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        from pa_agent.util.threading import CancelToken

        self._symbols = list(symbols)
        self._timeframe = timeframe
        self._bar_count = bar_count
        self._workers = workers
        self._mode = mode
        self._light_only = light_only
        self._light_bars = light_bars
        self._long_only = long_only
        self._min_conviction = min_conviction
        self._dual_screen = dual_screen
        self._use_local_gate = use_local_gate
        self._system_prompt = system_prompt
        self._user_template = user_template
        self._output_dir = output_dir
        self._cancel_token = CancelToken()
        _ACTIVE_THREADS.add(self)
        # 绑定到自身：即使对话框先死（连接按接收者自动断开），线程也能自回收
        self.finished.connect(lambda: _retire_thread(self))

    def request_stop(self) -> None:
        """GUI 线程调用：停止提交剩余股票并中止在飞任务（不阻塞等待）。"""
        self._cancel_token.set()

    def run(self) -> None:  # pragma: no cover - 工作线程主体
        from pa_agent.batch import screen_runner

        token = self._cancel_token
        total = len(self._symbols)
        results: list[dict] = []
        t0 = time.monotonic()
        cancelled = False

        def _submit(ex: ThreadPoolExecutor, futures: dict, queue: list) -> None:
            max_inflight = max(2 * self._workers, 4)
            while queue and len(futures) < max_inflight:
                sym = queue.pop(0)
                futures[ex.submit(
                    screen_runner.analyze_one, sym, self._timeframe, self._bar_count,
                    self._use_local_gate,
                    mode=self._mode, light_only=self._light_only,
                    light_bars=self._light_bars, long_only=self._long_only,
                    min_conviction=self._min_conviction,
                    dual_screen=self._dual_screen,
                    cancel_token=token,
                    system_prompt=self._system_prompt,
                    user_template=self._user_template,
                )] = sym

        try:
            with ThreadPoolExecutor(max_workers=self._workers,
                                    thread_name_prefix="screen") as ex:
                futures: dict = {}
                queue = list(self._symbols)
                _submit(ex, futures, queue)
                while futures:
                    done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                    for fut in done:
                        sym = futures.pop(fut)
                        try:
                            r = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            r = {"symbol": sym, "status": "failed", "error": str(exc),
                                 "timeframe": self._timeframe, "order_type": "",
                                 "reasoning": "", "light": None,
                                 "tokens_light": 0, "tokens_full": 0}
                        results.append(r)
                        light = r.get("light") or {}
                        line = "%s %s %s%s" % (
                            sym, _STATUS_LABELS.get(r.get("status"), "?"),
                            r.get("order_type") or "",
                            f" [{light['direction']}]"
                            if light and light.get("direction") else "",
                        )
                        self.result_ready.emit(r)
                        self.progress.emit(len(results), total, line)
                    if token.is_set():
                        cancelled = True
                    else:
                        _submit(ex, futures, queue)
        except Exception as exc:  # noqa: BLE001
            logger.exception("批量筛选线程异常")
            self.error_occurred.emit(str(exc))

        elapsed = time.monotonic() - t0
        out_dir = ""
        try:
            out_dir = str(screen_runner.write_screen_summary(
                self._output_dir, results,
                {
                    "timeframe": self._timeframe,
                    "bar_count": self._bar_count,
                    "mode": self._mode,
                    "light_only": self._light_only,
                    "dual_screen": self._dual_screen,
                    "min_conviction": self._min_conviction,
                    "duration_seconds": round(elapsed, 1),
                    "cancelled": cancelled,
                },
            ))
        except Exception as exc:  # noqa: BLE001
            logger.exception("summary.json 写盘失败")
            self.error_occurred.emit(f"结果写盘失败: {exc}")

        self.run_finished.emit({
            **screen_runner.summarize_results(results),
            "duration_seconds": round(elapsed, 1),
            "cancelled": cancelled,
            "out_dir": out_dir,
        })


class _LightPromptEditDialog(QDialog):
    """初筛提示词编辑：system / user 模板两个页签，可恢复默认。"""

    def __init__(self, settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from pa_agent.ai.light_screen import (
            LIGHT_SCREEN_SYSTEM_PROMPT,
            LIGHT_SCREEN_USER_TEMPLATE,
        )

        self._default_system = LIGHT_SCREEN_SYSTEM_PROMPT
        self._default_user = LIGHT_SCREEN_USER_TEMPLATE
        self._settings = settings

        self.setWindowTitle("编辑初筛提示词")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        note = QLabel(
            "system：初筛判定标准（通过/拒绝条件、输出格式），跨股票固定。\n"
            "user 模板：每股数据骨架。占位符 {symbol} {timeframe} {shown} {bg_count} "
            "{background_block} {detail_count} {kline_table} 会被实际数据替换；\n"
            "写错占位符或多余花括号时运行中自动回退默认。留空 = 使用内置默认。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        batch = getattr(settings, "batch", None)
        self._system_edit = QPlainTextEdit(getattr(batch, "light_system_prompt", "")
                                           or self._default_system)
        self._user_edit = QPlainTextEdit(getattr(batch, "light_user_template", "")
                                         or self._default_user)
        for edit in (self._system_edit, self._user_edit):
            edit.setStyleSheet(
                f"font-family: Cascadia Mono, Consolas, monospace;"
                f"font-size: {settings.general.stream_pane_font_pt}pt;"
            )

        tabs = QTabWidget()
        tabs.addTab(self._system_edit, "system（判定标准）")
        tabs.addTab(self._user_edit, "user 模板（数据骨架）")
        layout.addWidget(tabs, 1)

        from PyQt6.QtWidgets import QDialogButtonBox

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        restore = QPushButton("恢复默认")
        restore.clicked.connect(self._restore_defaults)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.addButton(restore, QDialogButtonBox.ButtonRole.ResetRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _restore_defaults(self) -> None:
        self._system_edit.setPlainText(self._default_system)
        self._user_edit.setPlainText(self._default_user)

    def apply_to_settings(self, settings) -> None:
        """把编辑结果写回 settings.batch（空输入=回到内置默认）。"""
        system = self._system_edit.toPlainText().strip()
        user = self._user_edit.toPlainText().strip()
        if system == self._default_system.strip():
            system = ""
        if user == self._default_user.strip():
            user = ""
        settings.batch.light_system_prompt = system
        settings.batch.light_user_template = user


class BatchScreenDialog(QDialog):
    """批量自选筛选主窗口（modeless）。"""

    def __init__(
        self,
        settings,
        *,
        get_timeframe: Callable[[], str],
        on_pick_symbol: Callable[[str], None],
        output_dir: str = "batch_analysis_results",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._get_timeframe = get_timeframe
        self._on_pick_symbol = on_pick_symbol
        self._output_dir = output_dir

        self._worker: BatchScreenWorker | None = None
        self._probe: _WatchlistProbeWorker | None = None
        self._symbols: list[str] = []
        self._last_out_dir = ""
        self._t0 = 0.0

        self.setWindowTitle("批量自选筛选")
        self.setModal(False)
        self.resize(980, 660)
        self._setup_ui()
        self._load_settings_to_ui()
        self._refresh_watchlist()

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._on_elapsed_tick)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        layout.addWidget(self._build_config_group())
        layout.addWidget(self._build_control_row())
        layout.addWidget(self._build_table(), 1)
        layout.addWidget(self._build_footer())

    def _build_config_group(self) -> QWidget:
        box = QGroupBox("运行配置")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        watch_row = QHBoxLayout()
        self._watchlist_label = QLabel("自选：读取中…")
        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedWidth(32)
        refresh_btn.setToolTip("重新读取通达信自选股")
        refresh_btn.clicked.connect(self._refresh_watchlist)
        watch_row.addWidget(self._watchlist_label)
        watch_row.addWidget(refresh_btn)
        watch_row.addStretch(1)
        form.addRow("自选列表", watch_row)

        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 30)
        self._workers_spin.setToolTip("并发线程数；网关限流出现失败时调低")
        form.addRow("线程数", self._workers_spin)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("轻量初筛 → 深析（省 token）", "light")
        self._mode_combo.addItem("全部完整两阶段", "full")
        form.addRow("模式", self._mode_combo)

        flags_row = QHBoxLayout()
        self._long_only_check = QCheckBox("只做多（空头淘汰）")
        self._long_only_check.setToolTip("A股无便利做空；初筛方向为空头的不深析")
        self._light_only_check = QCheckBox("仅初筛（不深析）")
        self._light_only_check.setToolTip("只跑轻量初筛，校准提示词/省钱巡视用")
        self._dual_check = QCheckBox("双初筛共识（更严）")
        self._dual_check.setToolTip(
            "Brooks 主筛 + 复核官两套标准并行、双通过才深析；token ×2 但过滤更严"
        )
        flags_row.addWidget(self._long_only_check)
        flags_row.addWidget(self._light_only_check)
        flags_row.addWidget(self._dual_check)
        flags_row.addStretch(1)
        form.addRow("过滤", flags_row)

        self._light_bars_spin = QSpinBox()
        self._light_bars_spin.setRange(20, 200)
        self._light_bars_spin.setToolTip("初筛用的已收盘 K 线根数")
        form.addRow("初筛 K 线数", self._light_bars_spin)

        self._min_conviction_spin = QSpinBox()
        self._min_conviction_spin.setRange(0, 100)
        self._min_conviction_spin.setSpecialValueText("关闭")
        self._min_conviction_spin.setToolTip(
            "初筛 conviction（0-100 信心分）门槛；低于门槛不深析省 token（0=不过滤）"
        )
        form.addRow("conviction 门槛", self._min_conviction_spin)

        prompt_row = QHBoxLayout()
        self._prompt_btn = QPushButton("编辑初筛提示词…")
        self._prompt_btn.clicked.connect(self._edit_prompts)
        self._prompt_state = QLabel("默认")
        self._prompt_state.setObjectName("mutedLabel")
        prompt_row.addWidget(self._prompt_btn)
        prompt_row.addWidget(self._prompt_state)
        prompt_row.addStretch(1)
        form.addRow("初筛提示词", prompt_row)

        return box

    def _build_control_row(self) -> QWidget:
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        self._start_btn = QPushButton("▶ 开始")
        self._start_btn.clicked.connect(self._on_start)
        self._start_btn.setStyleSheet(
            f"padding: 6px 22px; font-weight: bold; color: {T.ACCENT};"
        )
        row.addWidget(self._start_btn)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(8)
        row.addWidget(self._progress_bar, 1)

        self._progress_label = QLabel("— / —")
        self._progress_label.setObjectName("mutedLabel")
        row.addWidget(self._progress_label)

        self._elapsed_label = QLabel("")
        self._elapsed_label.setObjectName("mutedLabel")
        row.addWidget(self._elapsed_label)
        return wrap

    def _build_table(self) -> QWidget:
        self._table = QTableWidget(0, len(_TABLE_COLUMNS))
        self._table.setObjectName("batchResultTable")
        from pa_agent.gui.decision_tree_panel import _apply_dark_data_palette

        _apply_dark_data_palette(self._table)
        self._table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        hdr = self._table.horizontalHeader()
        for col in range(len(_TABLE_COLUMNS) - 1):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(len(_TABLE_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.setSortingEnabled(True)
        return self._table

    def _build_footer(self) -> QWidget:
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        self._summary_label = QLabel("尚未运行")
        self._summary_label.setWordWrap(True)
        self._summary_label.setObjectName("mutedLabel")
        row.addWidget(self._summary_label, 1)
        self._open_dir_btn = QPushButton("打开结果目录")
        self._open_dir_btn.setEnabled(False)
        self._open_dir_btn.clicked.connect(self._open_out_dir)
        row.addWidget(self._open_dir_btn)
        return wrap

    # ── 配置读写 ─────────────────────────────────────────────────────────────

    def _load_settings_to_ui(self) -> None:
        batch = getattr(self._settings, "batch", None)
        if batch is None:
            return
        self._workers_spin.setValue(batch.workers)
        idx = self._mode_combo.findData(batch.mode)
        self._mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._long_only_check.setChecked(batch.long_only)
        self._light_only_check.setChecked(batch.light_only)
        self._dual_check.setChecked(batch.dual_screen)
        self._light_bars_spin.setValue(batch.light_bars)
        self._min_conviction_spin.setValue(batch.min_conviction)
        self._update_prompt_state_label()

    def _save_ui_to_settings(self) -> None:
        from pa_agent.config.settings import save_settings

        batch = self._settings.batch
        batch.workers = self._workers_spin.value()
        batch.mode = self._mode_combo.currentData()
        batch.long_only = self._long_only_check.isChecked()
        batch.light_only = self._light_only_check.isChecked()
        batch.dual_screen = self._dual_check.isChecked()
        batch.light_bars = self._light_bars_spin.value()
        batch.min_conviction = self._min_conviction_spin.value()
        save_settings(self._settings)
        self._update_prompt_state_label()

    def _update_prompt_state_label(self) -> None:
        batch = getattr(self._settings, "batch", None)
        customized = bool(batch and (batch.light_system_prompt or batch.light_user_template))
        self._prompt_state.setText("已自定义" if customized else "默认")

    def _edit_prompts(self) -> None:
        dlg = _LightPromptEditDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            dlg.apply_to_settings(self._settings)
            from pa_agent.config.settings import save_settings

            save_settings(self._settings)
            self._update_prompt_state_label()

    # ── 自选读取 ─────────────────────────────────────────────────────────────

    def _refresh_watchlist(self) -> None:
        if self._probe is not None:
            return
        self._watchlist_label.setText("自选：读取中…")
        self._probe = _WatchlistProbeWorker()
        self._probe.ready.connect(self._on_watchlist_ready)
        self._probe.failed.connect(self._on_watchlist_failed)
        self._probe.finished.connect(self._on_probe_finished)
        self._probe.start()

    def _on_probe_finished(self) -> None:
        self._probe = None

    def _on_watchlist_ready(self, symbols: list) -> None:
        self._symbols = [str(s) for s in symbols]
        self._watchlist_label.setText(f"自选：{len(self._symbols)} 只")

    def _on_watchlist_failed(self, err: str) -> None:
        self._symbols = []
        self._watchlist_label.setText(f"自选：读取失败（{err[:60]}）")

    # ── 运行控制 ─────────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self._start_btn.setEnabled(False)
            self._start_btn.setText("停止中…")
            return

        if not self._symbols:
            QMessageBox.warning(self, "批量自选筛选", "自选列表为空（或尚未读取完成）")
            return

        self._save_ui_to_settings()
        batch = self._settings.batch
        timeframe = (self._get_timeframe() or "1h").strip() or "1h"
        bar_count = int(getattr(self._settings.general, "analysis_bar_count", 100) or 100)

        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._summary_label.setText("运行中…")
        self._open_dir_btn.setEnabled(False)
        self._progress_bar.setRange(0, len(self._symbols))
        self._progress_bar.setValue(0)
        self._progress_label.setText(f"0 / {len(self._symbols)}")
        self._t0 = time.monotonic()
        self._elapsed_timer.start()

        self._worker = BatchScreenWorker(
            self._symbols,
            timeframe=timeframe,
            bar_count=bar_count,
            workers=batch.workers,
            mode=batch.mode,
            light_only=batch.light_only,
            light_bars=batch.light_bars,
            long_only=batch.long_only,
            min_conviction=batch.min_conviction,
            dual_screen=batch.dual_screen,
            system_prompt=batch.light_system_prompt,
            user_template=batch.light_user_template,
            output_dir=self._output_dir,
        )
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.progress.connect(self._on_progress)
        self._worker.run_finished.connect(self._on_run_finished)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.finished.connect(self._on_worker_thread_finished)
        self._worker.start()

        self._start_btn.setText("⏹ 停止")
        self._start_btn.setStyleSheet(
            f"padding: 6px 22px; font-weight: bold; color: {T.DANGER};"
        )

    def _on_elapsed_tick(self) -> None:
        if self._worker is None:
            return
        secs = int(time.monotonic() - self._t0)
        self._elapsed_label.setText(f"⏱ {secs // 60}:{secs % 60:02d}")

    def _on_result_ready(self, r: dict) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        light = r.get("light") or {}
        status = str(r.get("status") or "?")

        def _num(v) -> str:
            return f"{v:.2f}" if isinstance(v, (int, float)) else (str(v) if v else "—")

        direction = {"bullish": "多", "bearish": "空", "neutral": "中"}.get(
            str(light.get("direction") or ""), ""
        )
        values = [
            str(r.get("symbol") or ""),
            _STATUS_LABELS.get(status, status),
            direction,
            str(light.get("conviction")) if light.get("conviction") is not None else "",
            str(r.get("order_type") or ""),
            _num(r.get("entry_price")),
            _num(r.get("stop_loss_price")),
            _num(r.get("take_profit_price")),
            str(r.get("trade_confidence"))
            if r.get("trade_confidence") is not None else "",
            (r.get("reasoning") or r.get("error") or "").replace("\n", " ")[:80],
        ]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, values[0])
            if col == 1:
                color = _STATUS_FOREGROUND.get(status)
                if color:
                    item.setForeground(QColor(color))
            if col in (3, 5, 6, 7, 8):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            self._table.setItem(row, col, item)
        self._table.scrollToBottom()

    def _on_progress(self, done: int, total: int, line: str) -> None:
        self._progress_bar.setValue(done)
        self._progress_label.setText(f"{done} / {total}")

    def _on_worker_error(self, err: str) -> None:
        self._summary_label.setText(f"⚠ {err}")

    def _on_worker_thread_finished(self) -> None:
        # QThread.run 结束：线程经自身 _retire_thread 自回收，这里只断对话框引用
        self._worker = None

    def _on_run_finished(self, summary: dict) -> None:
        self._last_summary = summary
        self._elapsed_timer.stop()
        self._table.setSortingEnabled(True)
        self._start_btn.setEnabled(True)
        self._start_btn.setText("▶ 开始")
        self._start_btn.setStyleSheet(
            f"padding: 6px 22px; font-weight: bold; color: {T.ACCENT};"
        )
        tokens = (summary.get("tokens_light") or 0) + (summary.get("tokens_full") or 0)
        mins = (summary.get("duration_seconds") or 0) / 60
        cancelled = "（已取消，仅计入已跑部分）" if summary.get("cancelled") else ""
        self._summary_label.setText(
            f"完成 {summary.get('total', 0)} 只{cancelled} · ★机会 {summary.get('opportunities', 0)}"
            f" · 观望 {summary.get('no_order', 0)} · 淘汰 {summary.get('screened_out', 0)}"
            f" · 停板 {summary.get('limit_skip', 0)} · 失败 {summary.get('failed', 0)}"
            f" · token {tokens / 10000:.1f} 万 · 耗时 {mins:.1f} 分钟"
        )
        out_dir = summary.get("out_dir") or ""
        if out_dir:
            self._last_out_dir = out_dir
            self._open_dir_btn.setEnabled(True)

    def _open_out_dir(self) -> None:
        if self._last_out_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_out_dir))

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        symbol = item.data(Qt.ItemDataRole.UserRole) or item.text()
        if symbol:
            self._on_pick_symbol(str(symbol))

    # ── 关闭保护 ─────────────────────────────────────────────────────────────

    def reject(self) -> None:  # Esc 键
        if not self._confirm_close_while_running():
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if not self._confirm_close_while_running():
            event.ignore()
            return
        super().closeEvent(event)

    def _confirm_close_while_running(self) -> bool:
        """运行中关闭：询问并停止（置 token、断信号；不阻塞等待线程结束）。"""
        worker = self._worker
        if worker is None:
            return True
        ret = QMessageBox.question(
            self, "批量自选筛选",
            "批量筛选仍在运行。停止并关闭？\n（已完成的股票结果仍会写入 summary.json）",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return False
        worker.request_stop()
        try:
            worker.result_ready.disconnect()
            worker.progress.disconnect()
            worker.run_finished.disconnect()
            worker.error_occurred.disconnect()
        except TypeError:
            pass  # 已断开
        self._elapsed_timer.stop()
        # worker 线程自然结束后经 finished → deleteLater 回收（僵尸模式，不 wait）
        return True
