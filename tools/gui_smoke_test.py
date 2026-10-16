"""界面冒烟测试：不点鼠标，程序化跑一遍「扫描 → 勾选 → 转换 → 收尾」。

用法：
    python tools/gui_smoke_test.py <源目录> <输出目录> [转换个数]
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gui  # noqa: E402

# 把弹窗换成记录，避免自动化测试被对话框卡住
DIALOGS: list = []
gui.messagebox.showinfo = lambda *a, **k: DIALOGS.append(("info", a))
gui.messagebox.showwarning = lambda *a, **k: DIALOGS.append(("warn", a))
gui.messagebox.showerror = lambda *a, **k: DIALOGS.append(("error", a))
gui.messagebox.askyesno = lambda *a, **k: True


def pump(root: tk.Tk, condition, timeout: float, label: str) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if not condition():
            return True
        time.sleep(0.05)
    root.update()
    print(f"✘ 等待超时：{label}")
    return False


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else "."
    output = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.getcwd(), "gui_test_output")
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    root = tk.Tk()
    root.title("ncm2mp3 冒烟测试")
    root.geometry("1000x760")
    app = gui.App(root)
    print("✔ 界面构造成功")

    app.var_source.set(source)
    app.var_output.set(output)
    app.var_engine.set(app.engine_labels["builtin"])
    app.var_jobs.set(4)

    print("→ 开始扫描")
    app._start_scan()
    if not pump(root, lambda: app.scanning, 300, "扫描"):
        root.destroy()
        return 1
    print(f"✔ 扫描完成，共 {len(app.items)} 个文件，列表行数 {len(app.tree.get_children())}")
    if len(app.items) != len(app.tree.get_children()):
        print("✘ 列表行数与文件数不一致")
        root.destroy()
        return 1

    # 全选 / 全不选 / 反选
    app._set_all(True)
    assert all(app.checked.values()), "全选失败"
    app._set_all(False)
    assert not any(app.checked.values()), "全不选失败"
    app._invert()
    assert all(app.checked.values()), "反选失败"
    print("✔ 全选 / 全不选 / 反选 正常")

    # 只勾前 N 个
    app._set_all(False)
    for index in range(min(count, len(app.items))):
        app.checked[index] = True
        app._refresh_row(index)
    print(f"→ 勾选前 {min(count, len(app.items))} 个文件并开始转换")

    app._start_convert()
    if not pump(root, lambda: app.running, 900, "转换"):
        root.destroy()
        return 1
    pump(root, lambda: False, 1.0, "收尾")  # 让最后的事件处理完

    text = app.log.get("1.0", "end")
    print("--- 日志尾部 ---")
    print("\n".join(line for line in text.strip().splitlines()[-12:]))
    print("--- 状态 ---")
    print(app.var_status.get())
    print("弹窗：", DIALOGS)

    produced = []
    for dirpath, _dirs, files in os.walk(output):
        for name in files:
            full = os.path.join(dirpath, name)
            if name.lower().endswith((".mp3", ".flac")):
                produced.append((os.path.getsize(full), full))
    print(f"✔ 生成 {len(produced)} 个音频文件")
    for size, path in sorted(produced):
        print(f"    {size:>12,}  {os.path.relpath(path, output)}")

    root.destroy()
    return 0 if produced else 1


if __name__ == "__main__":
    raise SystemExit(main())
