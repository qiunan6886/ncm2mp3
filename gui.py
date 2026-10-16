"""ncm2mp3 图形界面（Windows 桌面程序）。

功能：
* 选择目录 → 扫描出所有 .ncm → 列表里勾选要转换的文件（点选/全选/反选）；
* 一键转换，默认输出到源文件所在目录，也可以指定输出目录；
* 保持原始格式与音质：mp3 出来还是 mp3，flac 出来还是 flac，不重新编码；
* 自动写入标题/歌手/专辑/封面标签，转换后自检；
* 实时进度、逐文件状态、日志，可随时停止。

只依赖 Python 标准库（tkinter），不需要装任何第三方包。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ncm2mp3 import dedup  # noqa: E402
from ncm2mp3.core import (  # noqa: E402
    CANCELED, FAILED, OK, SKIPPED, ConvertOptions, Converter, SourceItem,
    describe_format, expected_output_path, find_existing_audio, human_size, scan,
)
from ncm2mp3.engines import engine_choices, find_official_dll  # noqa: E402

if getattr(sys, "frozen", False):
    # 用 PyInstaller 打包成 exe 运行时，配置要写在 exe 旁边，而不是临时解包目录
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
APP_TITLE = "ncm2mp3 —— 网易云 NCM 转换器（保持原始格式与音质）"

CHECKED = "☑"
UNCHECKED = "☐"


def open_in_explorer(path: str) -> None:
    try:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif os.path.isdir(path):
            os.startfile(os.path.normpath(path))  # noqa: S606
    except Exception:
        pass


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=8)
        self.master = master
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

        # 运行时状态
        self.items: list[SourceItem] = []
        self.row_ids: list[str] = []
        self.checked: dict[int, bool] = {}
        self.statuses: dict[int, str] = {}
        self.events: queue.Queue = queue.Queue()
        self.converter: Converter | None = None
        self.running = False
        self.scanning = False
        self.done_count = 0
        self.total_count = 0
        self.engine_values: dict[str, str] = {}   # 显示文字 → 引擎值
        self.engine_labels: dict[str, str] = {}   # 引擎值 → 显示文字

        self._build_vars()
        self._build_ui()
        self._load_config()
        self._update_summary()
        self._pump()
        master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # 界面构造
    # ------------------------------------------------------------------ #
    def _build_vars(self) -> None:
        self.var_source = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_keep_structure = tk.BooleanVar(value=True)
        self.var_artist_folder = tk.BooleanVar(value=False)
        self.var_rename = tk.BooleanVar(value=False)
        self.var_overwrite = tk.BooleanVar(value=False)
        self.var_delete = tk.BooleanVar(value=False)
        self.var_tags = tk.BooleanVar(value=True)
        self.var_cover = tk.BooleanVar(value=False)
        self.var_verify = tk.BooleanVar(value=True)
        self.var_jobs = tk.IntVar(value=4)
        self.var_engine = tk.StringVar()
        self.var_status = tk.StringVar(value="就绪")
        self.var_overall = tk.DoubleVar(value=0.0)
        self.var_current = tk.DoubleVar(value=0.0)

    def _build_ui(self) -> None:
        for column in range(2):
            self.columnconfigure(column, weight=1 if column == 0 else 0)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(7, weight=2)

        self._build_source_row()
        self._build_toolbar()
        self._build_tree()
        self._build_options()
        self._build_actions()
        self._build_progress()
        self._build_log()

    def _build_source_row(self) -> None:
        box = ttk.LabelFrame(self, text="① 选择目录", padding=6)
        box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        box.columnconfigure(0, weight=1)

        ttk.Entry(box, textvariable=self.var_source).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(box, text="浏览…", command=self._choose_source).grid(row=0, column=1, padx=2)
        self.btn_scan = ttk.Button(box, text="扫描 ncm", command=self._start_scan)
        self.btn_scan.grid(row=0, column=2, padx=2)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="全选", command=lambda: self._set_all(True)).pack(side="left", padx=2)
        ttk.Button(bar, text="全不选", command=lambda: self._set_all(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="反选", command=self._invert).pack(side="left", padx=2)
        ttk.Button(bar, text="清空列表", command=self._clear).pack(side="left", padx=2)
        self.var_summary = tk.StringVar(value="尚未扫描")
        ttk.Label(bar, textvariable=self.var_summary).pack(side="right", padx=6)
        self.var_existing = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_existing, foreground="#1a7f37").pack(side="right", padx=6)
        ttk.Label(bar, text="（单击最左侧方框或双击行可勾选/取消）", foreground="#666").pack(side="left", padx=12)

    def _build_tree(self) -> None:
        wrap = ttk.Frame(self)
        wrap.grid(row=2, column=0, columnspan=2, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        columns = ("check", "name", "fmt", "duration", "size", "status")
        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
        headings = {
            "check": ("选", 44, "center"),
            "name": ("文件名", 360, "w"),
            "fmt": ("格式", 70, "center"),
            "duration": ("时长", 70, "center"),
            "size": ("大小", 90, "e"),
            "status": ("状态", 150, "w"),
        }
        for key, (text, width, anchor) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))

        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Control-a>", lambda e: (self._set_all(True), "break")[1])

    def _build_options(self) -> None:
        box = ttk.LabelFrame(self, text="② 输出设置（默认输出到源文件所在目录）", padding=6)
        box.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="输出目录").grid(row=0, column=0, sticky="w")
        self.entry_output = ttk.Entry(box, textvariable=self.var_output)
        self.entry_output.grid(row=0, column=1, sticky="ew", padx=6)
        self.entry_output.bind("<FocusOut>", lambda _e: self._refresh_existing_marks())
        self.entry_output.bind("<Return>", lambda _e: self._refresh_existing_marks())
        ttk.Button(box, text="浏览…", command=self._choose_output).grid(row=0, column=2, padx=2)
        ttk.Button(box, text="用源目录", command=self._use_source_dir).grid(row=0, column=3, padx=2)

        opt = ttk.Frame(box)
        opt.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(opt, text="保留子目录结构", variable=self.var_keep_structure,
                        command=self._refresh_existing_marks).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="用「歌手 - 歌名」命名", variable=self.var_rename,
                        command=self._refresh_existing_marks).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="直接放在源目录的歌按歌手归档", variable=self.var_artist_folder,
                        command=self._refresh_existing_marks).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="覆盖已存在文件", variable=self.var_overwrite,
                        command=self._refresh_existing_marks).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="写入标签与封面", variable=self.var_tags).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="另外导出封面图片", variable=self.var_cover).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="转换后校验", variable=self.var_verify).pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="成功后删除原 ncm", variable=self.var_delete).pack(side="left", padx=4)

        row2 = ttk.Frame(box)
        row2.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(row2, text="转换引擎").pack(side="left")
        self.combo_engine = ttk.Combobox(row2, textvariable=self.var_engine, state="readonly", width=44)
        choices = engine_choices()
        for value, label, available in choices:
            text = label if available else f"{label}（缺少 dll，不可用）"
            self.engine_values[text] = value
            self.engine_labels[value] = text
        self.combo_engine["values"] = list(self.engine_values.keys())
        self.var_engine.set(self.engine_labels.get("builtin", ""))
        self.combo_engine.pack(side="left", padx=6)
        self.combo_engine.bind("<<ComboboxSelected>>", lambda e: self._update_engine_hint())
        ttk.Label(row2, text="并发数").pack(side="left", padx=(16, 2))
        ttk.Spinbox(row2, from_=1, to=16, width=4, textvariable=self.var_jobs).pack(side="left")
        self.lbl_engine_hint = ttk.Label(row2, text="", foreground="#666")
        self.lbl_engine_hint.pack(side="left", padx=12)
        self._update_engine_hint()

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 2))
        self.btn_start = ttk.Button(bar, text="▶  开始转换", command=self._start_convert)
        self.btn_start.pack(side="left", padx=2)
        self.btn_stop = ttk.Button(bar, text="■  停止", command=self._stop_convert, state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        ttk.Button(bar, text="打开输出目录", command=self._open_output).pack(side="left", padx=12)
        self.btn_dedup = ttk.Button(bar, text="🧹  清理重复音乐", command=self._open_dedup)
        self.btn_dedup.pack(side="left", padx=2)

    def _build_progress(self) -> None:
        box = ttk.Frame(self)
        box.grid(row=5, column=0, columnspan=2, sticky="ew")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="总进度").grid(row=0, column=0, sticky="w")
        self.bar_overall = ttk.Progressbar(box, variable=self.var_overall, maximum=100)
        self.bar_overall.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(box, text="当前文件").grid(row=1, column=0, sticky="w")
        self.bar_current = ttk.Progressbar(box, variable=self.var_current, maximum=100)
        self.bar_current.grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Label(self, textvariable=self.var_status, foreground="#1a5fb4").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(4, 2))

    def _build_log(self) -> None:
        box = ttk.LabelFrame(self, text="日志", padding=4)
        box.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.log = ScrolledText(box, height=8, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------------ #
    # 勾选逻辑
    # ------------------------------------------------------------------ #
    def _toggle_row(self, row_id: str) -> None:
        try:
            index = self.row_ids.index(row_id)
        except ValueError:
            return
        self.checked[index] = not self.checked.get(index, False)
        self._refresh_row(index)
        self._update_summary()

    def _refresh_row(self, index: int) -> None:
        item = self.items[index]
        mark = CHECKED if self.checked.get(index) else UNCHECKED
        values = (
            mark, item.name, item.fmt_text, item.duration_text, item.size_text,
            self.statuses.get(index, "待转换" if not item.error else "无法解析"),
        )
        self.tree.item(self.row_ids[index], values=values)

    def _set_all(self, value: bool) -> None:
        for index in range(len(self.items)):
            self.checked[index] = value
            self._refresh_row(index)
        self._update_summary()

    def _invert(self) -> None:
        for index in range(len(self.items)):
            self.checked[index] = not self.checked.get(index, False)
            self._refresh_row(index)
        self._update_summary()

    def _on_tree_click(self, event) -> None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if row_id and column == "#1":
            self._toggle_row(row_id)

    def _on_tree_double(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self._toggle_row(row_id)

    def _on_space(self, _event) -> str:
        for row_id in self.tree.selection():
            self._toggle_row(row_id)
        return "break"

    def _clear(self) -> None:
        if self.running:
            messagebox.showinfo("提示", "转换进行中，无法清空列表")
            return
        self.tree.delete(*self.tree.get_children())
        self.items = []
        self.row_ids = []
        self.checked = {}
        self.statuses = {}
        self.var_overall.set(0.0)
        self.var_current.set(0.0)
        self.var_status.set("就绪")
        self._update_summary()

    def _update_summary(self) -> None:
        total = len(self.items)
        chosen = sum(1 for i in range(total) if self.checked.get(i))
        failed = sum(1 for item in self.items if item.error)
        text = f"共 {total} 个文件，已选 {chosen} 个"
        if failed:
            text += f"，其中 {failed} 个无法解析"
        self.var_summary.set(text)

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def _choose_source(self) -> None:
        path = filedialog.askdirectory(title="选择包含 ncm 的目录")
        if path:
            self.var_source.set(os.path.normpath(path))

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.var_output.set(os.path.normpath(path))
            self._refresh_existing_marks()

    def _use_source_dir(self) -> None:
        self.var_output.set("")
        self._refresh_existing_marks()

    def _current_options(self) -> ConvertOptions:
        """把界面上的设置收集成转换选项。"""
        engine = self.engine_values.get(self.var_engine.get(), "builtin")
        if engine == "official" and not find_official_dll():
            engine = "builtin"
        try:
            jobs = max(1, int(self.var_jobs.get() or 4))
        except (TypeError, ValueError):
            jobs = 4
        return ConvertOptions(
            output_dir=self.var_output.get().strip() or None,
            keep_structure=self.var_keep_structure.get(),
            use_song_name=self.var_rename.get(),
            overwrite=self.var_overwrite.get(),
            delete_source=self.var_delete.get(),
            write_tags=self.var_tags.get(),
            save_cover=self.var_cover.get(),
            verify=self.var_verify.get(),
            jobs=jobs,
            engine=engine,
            artist_subfolder=self.var_artist_folder.get(),
        )

    def _refresh_existing_marks(self) -> None:
        """按当前设置重新标记：哪些文件其实已经有转换结果了（转换时会跳过）。"""
        self.var_existing.set("")
        if not self.items or self.running:
            return
        options = self._current_options()
        existing = 0
        for index, item in enumerate(self.items):
            if item.error:
                continue
            status = self.statuses.get(index, "")
            if status and (status.startswith(("完成", "失败", "已取消")) or status in ("转换中…", "排队中")):
                continue
            found = None if options.overwrite else find_existing_audio(expected_output_path(item, options))
            if found:
                self.statuses[index] = "已存在，将跳过"
                existing += 1
            else:
                self.statuses[index] = "待转换"
            self._refresh_row(index)
        if existing:
            self.var_existing.set(f"其中 {existing} 个已有转换结果，将自动跳过")

    def _start_scan(self) -> None:
        if self.scanning or self.running:
            return
        source = self.var_source.get().strip()
        if not source:
            messagebox.showinfo("提示", "请先选择要扫描的目录")
            return
        if not os.path.isdir(source):
            messagebox.showerror("错误", f"目录不存在：{source}")
            return
        self.scanning = True
        self.btn_scan.configure(state="disabled")
        self.var_status.set("正在扫描…")
        self._append_log(f"扫描目录：{source}")

        def work() -> None:
            try:
                found = scan([source], log=lambda m: self.events.put(("log", m)))
                self.events.put(("scan_done", found))
            except Exception as exc:
                self.events.put(("log", f"扫描失败：{exc}"))
                self.events.put(("scan_done", []))

        threading.Thread(target=work, daemon=True).start()

    def _apply_scan(self, found: list[SourceItem]) -> None:
        self.scanning = False
        self.btn_scan.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.items = found
        self.row_ids = []
        self.checked = {}
        self.statuses = {}
        for index, item in enumerate(found):
            row_id = self.tree.insert("", "end", values=("",))
            self.row_ids.append(row_id)
            self.checked[index] = not item.error
            self._refresh_row(index)
        self._update_summary()
        self._refresh_existing_marks()
        if not found:
            self.var_status.set("没有找到 ncm 文件")
        else:
            formats: dict[str, int] = {}
            for item in found:
                key = describe_format(item.fmt) if not item.error else "无法解析"
                formats[key] = formats.get(key, 0) + 1
            detail = "，".join(f"{k} {v} 个" for k, v in formats.items())
            self.var_status.set(f"扫描完成：{len(found)} 个文件（{detail}）")
            self._append_log(f"扫描完成：{len(found)} 个文件（{detail}）")
            if not self.var_output.get().strip():
                self._append_log("输出目录：与源文件同一目录（可在②里修改）")

    # ------------------------------------------------------------------ #
    # 转换
    # ------------------------------------------------------------------ #
    def _selected_items(self) -> list[SourceItem]:
        return [item for index, item in enumerate(self.items) if self.checked.get(index)]

    def _start_convert(self) -> None:
        if self.running:
            return
        if self.scanning:
            messagebox.showinfo("提示", "正在扫描，请稍候")
            return
        items = self._selected_items()
        if not items:
            messagebox.showinfo("提示", "请先扫描目录并勾选要转换的文件")
            return

        output_dir = self.var_output.get().strip() or None
        if output_dir:
            try:
                os.makedirs(output_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("错误", f"无法创建输出目录：{exc}")
                return

        engine_label = self.var_engine.get()
        options = self._current_options()
        if self.engine_values.get(engine_label) == "official" and options.engine != "official":
            messagebox.showwarning("提示", "没有找到 libncmdump.dll，将使用内置引擎")
        self._save_config()

        self.running = True
        self.done_count = 0
        self.total_count = len(items)
        self.run_results: list = []
        self.var_overall.set(0.0)
        self.var_current.set(0.0)
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_scan.configure(state="disabled")
        index_of_path = {item.path: index for index, item in enumerate(self.items)}
        for item in items:
            position = index_of_path.get(item.path)
            if position is not None:
                self.statuses[position] = "排队中"
                self._refresh_row(position)

        self._append_log("=" * 68)
        self._append_log(f"开始转换 {len(items)} 个文件｜引擎：{engine_label}"
                         f"｜输出：{output_dir or '与源文件同一目录'}｜并发：{options.jobs}")
        self.var_status.set(f"转换中… 0/{len(items)}")

        def work() -> None:
            converter = Converter(options)
            self.converter = converter
            if converter.engine_note:
                self.events.put(("log", converter.engine_note))
            try:
                converter.run(
                    items,
                    on_start=lambda i, it: self.events.put(("file_start", i, it)),
                    on_progress=lambda i, frac: self.events.put(("file_progress", i, frac)),
                    on_done=lambda i, res: self.events.put(("file_done", i, res)),
                    log=lambda m: self.events.put(("log", m)),
                )
            except Exception as exc:
                self.events.put(("log", f"转换过程出错：{exc}"))
            finally:
                self.events.put(("all_done", None))

        threading.Thread(target=work, daemon=True).start()

    def _stop_convert(self) -> None:
        if self.converter is not None:
            self.converter.cancel()
            self.var_status.set("正在停止…（等待当前文件结束）")
            self._append_log("已请求停止，正在收尾…")
            self.btn_stop.configure(state="disabled")

    def _index_of(self, item: SourceItem) -> int:
        for index, candidate in enumerate(self.items):
            if candidate is item or candidate.path == item.path:
                return index
        return -1

    def _on_file_start(self, index: int, item: SourceItem) -> None:
        position = self._index_of(item)
        if position < 0:
            return
        self.statuses[position] = "转换中…"
        self._refresh_row(position)

    def _on_file_progress(self, index: int, frac: float) -> None:
        self.var_current.set(max(0.0, min(1.0, frac)) * 100.0)

    def _on_file_done(self, index: int, result) -> None:
        position = self._index_of_by_path(result.src)
        self.done_count += 1
        self.run_results.append(result)
        if position >= 0:
            text = {
                OK: f"完成  {result.seconds:.1f}s",
                SKIPPED: "已跳过",
                CANCELED: "已取消",
                FAILED: f"失败  {result.message[:40]}",
            }.get(result.status, result.status)
            self.statuses[position] = text
            self._refresh_row(position)
        self.var_overall.set(self.done_count / max(1, self.total_count) * 100.0)
        self.var_current.set(0.0)
        self.var_status.set(f"转换中… {self.done_count}/{self.total_count}")

    def _index_of_by_path(self, path: str) -> int:
        for index, candidate in enumerate(self.items):
            if candidate.path == path:
                return index
        return -1

    def _on_all_done(self) -> None:
        self.running = False
        self.converter = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_scan.configure(state="normal")
        results = getattr(self, "run_results", [])
        ok = sum(1 for r in results if r.status == OK)
        skipped = sum(1 for r in results if r.status == SKIPPED)
        canceled = sum(1 for r in results if r.status == CANCELED)
        failed = sum(1 for r in results if r.status == FAILED)
        self.var_current.set(0.0)
        self.var_status.set(f"结束：成功 {ok}｜跳过 {skipped}｜失败 {failed}"
                            + (f"｜取消 {canceled}" if canceled else ""))
        self._append_log(f"全部结束：成功 {ok}，跳过 {skipped}，失败 {failed}"
                         + (f"，取消 {canceled}" if canceled else ""))
        if failed:
            messagebox.showwarning("完成", f"成功 {ok} 个，跳过 {skipped} 个，失败 {failed} 个。\n详情见日志。")
        else:
            messagebox.showinfo("完成", f"成功 {ok} 个，跳过 {skipped} 个。")

    # ------------------------------------------------------------------ #
    # 事件泵
    # ------------------------------------------------------------------ #
    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1])
                elif kind == "scan_done":
                    self._apply_scan(event[1])
                elif kind == "file_start":
                    self._on_file_start(event[1], event[2])
                elif kind == "file_progress":
                    self._on_file_progress(event[1], event[2])
                elif kind == "file_done":
                    self._on_file_done(event[1], event[2])
                elif kind == "all_done":
                    self._on_all_done()
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_dedup(self) -> None:
        if self.running:
            messagebox.showinfo("提示", "转换进行中，请先等它结束或点「停止」")
            return
        library = self.var_output.get().strip() or self.var_source.get().strip()
        DedupDialog(self.master, library)

    def _open_output(self) -> None:
        target = self.var_output.get().strip()
        if not target:
            source = self.var_source.get().strip()
            if self.items:
                target = os.path.dirname(self.items[0].path)
            elif source:
                target = source
        if target and os.path.isdir(target):
            open_in_explorer(target)
        else:
            messagebox.showinfo("提示", "输出目录还不存在")

    def _update_engine_hint(self) -> None:
        value = self.engine_values.get(self.var_engine.get(), "builtin")
        if value == "official":
            self.lbl_engine_hint.configure(text="官方引擎不支持逐个文件的进度与中途停止")
        else:
            dll = find_official_dll()
            self.lbl_engine_hint.configure(
                text="内置引擎：有进度、可停止、转换后自检"
                     + (f"（已找到官方 dll：{os.path.basename(dll)}）" if dll else "")
            )

    # ------------------------------------------------------------------ #
    # 配置持久化
    # ------------------------------------------------------------------ #
    def _load_config(self) -> None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return
        self.var_source.set(data.get("source_dir", ""))
        self.var_output.set(data.get("output_dir", ""))
        self.var_keep_structure.set(bool(data.get("keep_structure", True)))
        self.var_artist_folder.set(bool(data.get("artist_subfolder", False)))
        self.var_rename.set(bool(data.get("use_song_name", False)))
        self.var_overwrite.set(bool(data.get("overwrite", False)))
        self.var_delete.set(bool(data.get("delete_source", False)))
        self.var_tags.set(bool(data.get("write_tags", True)))
        self.var_cover.set(bool(data.get("save_cover", False)))
        self.var_verify.set(bool(data.get("verify", True)))
        try:
            self.var_jobs.set(int(data.get("jobs", 4)))
        except (TypeError, ValueError):
            self.var_jobs.set(4)
        engine = data.get("engine", "builtin")
        if engine in self.engine_labels:
            self.var_engine.set(self.engine_labels[engine])
        if self.var_source.get() and os.path.isdir(self.var_source.get()):
            self._append_log(f"已载入上次的设置，源目录：{self.var_source.get()}")

    def _save_config(self) -> None:
        data = {
            "source_dir": self.var_source.get(),
            "output_dir": self.var_output.get(),
            "keep_structure": self.var_keep_structure.get(),
            "artist_subfolder": self.var_artist_folder.get(),
            "use_song_name": self.var_rename.get(),
            "overwrite": self.var_overwrite.get(),
            "delete_source": self.var_delete.get(),
            "write_tags": self.var_tags.get(),
            "save_cover": self.var_cover.get(),
            "verify": self.var_verify.get(),
            "jobs": int(self.var_jobs.get() or 4),
            "engine": self.engine_values.get(self.var_engine.get(), "builtin"),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("确认", "还在转换中，确定要退出吗？"):
                return
            if self.converter:
                self.converter.cancel()
        self._save_config()
        self.master.destroy()


class DedupDialog(tk.Toplevel):
    """清理重复音乐：先扫描预览，勾选后再把重复文件移入回收站。"""

    def __init__(self, master, initial_dir: str = ""):
        super().__init__(master)
        self.title("清理重复音乐")
        self.geometry("1120x660")
        self.minsize(900, 520)
        self.transient(master)

        self.groups: list = []
        self.row_ids: list[str] = []
        self.checked: dict[int, bool] = {}
        self.events: queue.Queue = queue.Queue()
        self.scanning = False
        self.var_dir = tk.StringVar(value=initial_dir)
        self.var_summary = tk.StringVar(value="尚未扫描")
        self.var_status = tk.StringVar(value="")

        self._build()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.after(80, self._pump)
        if initial_dir:
            self.after(200, self._start_scan)

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        top = ttk.LabelFrame(root, text="扫描目录（音乐库所在目录）", padding=6)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        ttk.Entry(top, textvariable=self.var_dir).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(top, text="浏览…", command=self._choose_dir).grid(row=0, column=1, padx=2)
        self.btn_scan = ttk.Button(top, text="扫描重复", command=self._start_scan)
        self.btn_scan.grid(row=0, column=2, padx=2)

        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="全选", command=lambda: self._set_all(True)).pack(side="left", padx=2)
        ttk.Button(bar, text="全不选", command=lambda: self._set_all(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="只选「内容相同」", command=self._select_safe).pack(side="left", padx=2)
        ttk.Label(bar, textvariable=self.var_summary, foreground="#1a7f37").pack(side="right")

        wrap = ttk.Frame(root)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        columns = ("check", "category", "keep", "remove", "free", "where")
        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
        for key, text, width, anchor in (
            ("check", "选", 44, "center"),
            ("category", "类别", 170, "w"),
            ("keep", "保留", 300, "w"),
            ("remove", "可删除", 330, "w"),
            ("free", "可释放", 90, "e"),
            ("where", "位置", 200, "w"),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("keep", "remove")))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<space>", self._on_space)

        ttk.Label(root, text="说明：删除的文件会移入 Windows 回收站，可随时还原；每个重复组都至少保留一个文件。"
                             "音频不同的「同名」文件默认不勾选，请自行确认后再决定。",
                  foreground="#666", wraplength=1080, justify="left").pack(fill="x", pady=(6, 0))

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, textvariable=self.var_status, foreground="#1a5fb4").pack(side="left")
        ttk.Button(bottom, text="关闭", command=self.destroy).pack(side="right", padx=4)
        self.btn_delete = ttk.Button(bottom, text="删除所选（移入回收站）",
                                     command=self._delete_selected, state="disabled")
        self.btn_delete.pack(side="right", padx=4)

    # ------------------------------------------------------------------ #
    def _choose_dir(self) -> None:
        path = filedialog.askdirectory(title="选择要清理的目录", parent=self)
        if path:
            self.var_dir.set(os.path.normpath(path))

    def _start_scan(self) -> None:
        path = self.var_dir.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showerror("错误", "请选择一个存在的目录", parent=self)
            return
        if self.scanning:
            return
        self.scanning = True
        self.btn_scan.configure(state="disabled")
        self.btn_delete.configure(state="disabled")
        self.var_summary.set("正在扫描…")
        self.var_status.set("正在扫描，重复文件会逐个比对音频内容，稍等…")

        def work() -> None:
            try:
                found = dedup.scan_duplicates(path)
                self.events.put(("scan_done", found))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_scan(self, groups: list) -> None:
        self.scanning = False
        self.btn_scan.configure(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.groups = groups
        self.row_ids = []
        self.checked = {}
        for index, group in enumerate(groups):
            row_id = self.tree.insert("", "end", values=("",))
            self.row_ids.append(row_id)
            self.checked[index] = group.auto
            self._refresh_row(index)
        self._update_summary()
        if not groups:
            self.var_status.set("没有发现重复文件")
        else:
            self.var_status.set(f"扫描完成：{len(groups)} 组重复")

    def _refresh_row(self, index: int) -> None:
        group = self.groups[index]
        if index >= len(self.row_ids):
            return
        try:
            where = os.path.relpath(group.keep.folder, self.var_dir.get())
        except ValueError:
            where = group.keep.folder
        values = (
            CHECKED if self.checked.get(index) else UNCHECKED,
            group.category,
            group.keep.name,
            group.describe_remove(2),
            human_size(group.free_bytes),
            "" if where == "." else where,
        )
        self.tree.item(self.row_ids[index], values=values)

    # ------------------------------------------------------------------ #
    def _toggle_row(self, row_id: str) -> None:
        try:
            index = self.row_ids.index(row_id)
        except ValueError:
            return
        self.checked[index] = not self.checked.get(index, False)
        self._refresh_row(index)
        self._update_summary()

    def _set_all(self, value: bool) -> None:
        for index in range(len(self.groups)):
            self.checked[index] = value
            self._refresh_row(index)
        self._update_summary()

    def _select_safe(self) -> None:
        for index, group in enumerate(self.groups):
            self.checked[index] = group.auto
            self._refresh_row(index)
        self._update_summary()

    def _on_click(self, event) -> None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        if row_id and self.tree.identify_column(event.x) == "#1":
            self._toggle_row(row_id)

    def _on_double(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self._toggle_row(row_id)

    def _on_space(self, _event) -> str:
        for row_id in self.tree.selection():
            self._toggle_row(row_id)
        return "break"

    def _update_summary(self) -> None:
        groups = sum(1 for i in range(len(self.groups)) if self.checked.get(i))
        files = sum(len(self.groups[i].remove) for i in range(len(self.groups)) if self.checked.get(i))
        size = sum(self.groups[i].free_bytes for i in range(len(self.groups)) if self.checked.get(i))
        self.var_summary.set(f"共 {len(self.groups)} 组重复｜已选 {groups} 组、{files} 个文件，可释放 {human_size(size)}")
        self.btn_delete.configure(state="normal" if files else "disabled")

    # ------------------------------------------------------------------ #
    def _delete_selected(self) -> None:
        targets: list[str] = []
        for index, group in enumerate(self.groups):
            if self.checked.get(index):
                targets.extend(item.path for item in group.remove)
        targets = [p for p in targets if os.path.exists(p)]
        if not targets:
            messagebox.showinfo("提示", "没有勾选任何要删除的文件", parent=self)
            return
        total = sum(os.path.getsize(p) for p in targets)
        if not messagebox.askyesno(
            "确认删除",
            f"将把 {len(targets)} 个文件（约 {human_size(total)}）移入 Windows 回收站。\n\n"
            "· 每个重复组都会保留一份\n· 文件可随时从回收站还原\n\n确定继续吗？",
            parent=self,
        ):
            return
        moved, failed = dedup.move_to_recycle_bin(targets)
        self.var_status.set(f"已移入回收站 {moved} 个文件，释放 {human_size(total)}"
                            + (f"，失败 {len(failed)} 个" if failed else ""))
        if failed:
            messagebox.showwarning("部分失败", "以下文件没能删除：\n" + "\n".join(failed[:10]), parent=self)
        self._start_scan()

    # ------------------------------------------------------------------ #
    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "scan_done":
                    self._apply_scan(event[1])
                elif event[0] == "error":
                    self.scanning = False
                    self.btn_scan.configure(state="normal")
                    self.var_status.set(f"扫描失败：{event[1]}")
                    messagebox.showerror("扫描失败", str(event[1]), parent=self)
        except queue.Empty:
            pass
        self.after(80, self._pump)


def main() -> int:
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1000x760")
    root.minsize(860, 640)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
