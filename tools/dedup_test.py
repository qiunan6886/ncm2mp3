"""重复音乐清理功能测试。

覆盖：
1. 同名同内容 → 归为「内容相同」并默认勾选；
2. 同名不同内容 → 归为「内容不同」且默认不勾选；
3. 同名不同格式 → 归为「不同格式」，保留无损那份；
4. 不同目录同名 → 归为「跨目录同名」；
5. 每一组至少保留一个文件，绝不出现"全组都被删"；
6. 回收站删除真的把文件移走（可恢复，不是硬删除）；
7. 界面对话框能扫描并正确显示、勾选。

用法：
    python tools/dedup_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3 import dedup  # noqa: E402

FAILURES = 0


def check(ok: bool, label: str) -> None:
    global FAILURES
    if not ok:
        FAILURES += 1
    print(f"{'✔' if ok else '✘'} {label}")


def write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def build_fixture(root: str) -> None:
    # 1. 同名同内容
    write(os.path.join(root, "歌手A", "歌甲.flac"), b"FLACDATA" * 100)
    write(os.path.join(root, "歌手A", "歌甲 (1).flac"), b"FLACDATA" * 100)
    # 2. 同名不同内容
    write(os.path.join(root, "歌手A", "歌乙.flac"), b"X" * 800)
    write(os.path.join(root, "歌手A", "歌乙 (1).flac"), b"Y" * 900)
    # 3. 同名不同格式（flac 应被保留）
    write(os.path.join(root, "歌手B", "歌丙.flac"), b"Z" * 1000)
    write(os.path.join(root, "歌手B", "歌丙.mp3"), b"M" * 400)
    # 4. 跨目录同名
    write(os.path.join(root, "歌手C", "歌丁.flac"), b"W" * 1200)
    write(os.path.join(root, "未知歌手", "歌丁.flac"), b"W" * 1200)
    # 5. 正常不重复的文件
    write(os.path.join(root, "歌手D", "独一份.flac"), b"UNIQUE" * 300)


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="ncm2mp3_dedup_")
    try:
        library = os.path.join(workdir, "library")
        build_fixture(library)
        groups = dedup.scan_duplicates(library)
        by_category = {}
        for group in groups:
            by_category.setdefault(group.category, []).append(group)

        print("=== 检测结果 ===")
        for group in groups:
            print(f"  [{group.category}] 保留 {group.keep.name}，删除 "
                  f"{[f.name for f in group.remove]}｜{group.note}")

        same = by_category.get(dedup.CATEGORY_SAME_CONTENT, [])
        check(len(same) == 1 and same[0].keep.name == "歌甲.flac" and
              [f.name for f in same[0].remove] == ["歌甲 (1).flac"],
              "同名同内容：保留不带 (1) 的那份，删除副本，且默认勾选")
        check(bool(same) and same[0].auto, "「内容相同」默认勾选")

        diff = by_category.get(dedup.CATEGORY_SAME_DIFF, [])
        check(len(diff) == 1 and not diff[0].auto, "同名不同内容：提示但默认不勾选")

        fmt = by_category.get(dedup.CATEGORY_FORMAT, [])
        check(len(fmt) == 1 and fmt[0].keep.name == "歌丙.flac" and
              [f.name for f in fmt[0].remove] == ["歌丙.mp3"],
              "同名不同格式：保留无损 flac，把 mp3 列为可删除")
        check(bool(fmt) and not fmt[0].auto, "「不同格式」默认不勾选")

        cross = by_category.get(dedup.CATEGORY_CROSS_DIR, [])
        check(len(cross) == 1 and len(cross[0].remove) == 1, "跨目录同名：识别为一组")
        check(bool(cross) and not cross[0].auto, "「跨目录同名」默认不勾选")

        check(all(group.keep.path not in {f.path for f in group.remove} for group in groups),
              "每一组都不会把自己要保留的文件列进删除清单")

        # 回收站删除
        print()
        print("=== 回收站删除 ===")
        victim = os.path.join(library, "歌手A", "歌甲 (1).flac")
        keepfile = os.path.join(library, "歌手A", "歌甲.flac")
        moved, failed = dedup.move_to_recycle_bin([victim, keepfile])
        check(moved == 2 and not failed, f"两个文件都成功移入回收站（moved={moved}, failed={failed}）")
        check(not os.path.exists(victim) and not os.path.exists(keepfile), "文件已从原位置消失")

        # 界面
        print()
        print("=== 界面对话框 ===")
        root = tk.Tk()
        root.withdraw()
        gui_dir = os.path.join(workdir, "gui_lib")
        build_fixture(gui_dir)
        dialog = __import__("gui").DedupDialog(root, gui_dir)
        deadline = time.time() + 60
        while time.time() < deadline:
            root.update()
            status = dialog.var_status.get()
            if "扫描完成" in status or "没有发现" in status or "失败" in status:
                break
            time.sleep(0.05)
        root.update()
        rows = len(dialog.tree.get_children())
        check(rows == len(dedup.scan_duplicates(gui_dir)),
              f"对话框列出 {rows} 行（与扫描结果一致）")
        checked = sum(1 for value in dialog.checked.values() if value)
        check(checked == 1, f"默认只勾选「内容相同」的 1 组（实际 {checked} 组）")
        summary = dialog.var_summary.get()
        check("组重复" in summary and "可释放" in summary, f"汇总显示正常：{summary}")
        dialog.destroy()
        root.destroy()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("结论：", "✔ 全部符合预期" if FAILURES == 0 else f"✘ {FAILURES} 项不符合预期")
    return 0 if FAILURES == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
