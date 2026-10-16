"""健壮性测试：转换途中取消，检查是否留下半成品/临时文件。

用法：
    python tools/cancel_test.py <源目录> [取消延迟秒数]
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.core import CANCELED, OK, ConvertOptions, Converter, scan  # noqa: E402

OUTPUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_cancel_test")


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else "."
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.3

    shutil.rmtree(OUTPUT, ignore_errors=True)
    items = scan([source])
    if not items:
        print("没有可用的 ncm")
        return 2
    # 挑最大的几个文件，保证取消时转换还在进行中
    items.sort(key=lambda i: i.audio_size, reverse=True)
    items = items[:6]
    print(f"挑中 {len(items)} 个大文件，最大 {items[0].audio_size / 1024 / 1024:.0f} MB")

    converter = Converter(ConvertOptions(output_dir=OUTPUT, overwrite=True, verify=False, jobs=2))
    results: list = []

    thread = threading.Thread(target=lambda: results.extend(converter.run(items)))
    thread.start()
    time.sleep(delay)
    converter.cancel()
    print(f"已发出取消请求（延迟 {delay}s）")
    thread.join(timeout=300)
    print(f"线程结束：{'是' if not thread.is_alive() else '否（卡住！）'}")

    statuses = [r.status for r in results]
    print("结果状态：", statuses)
    print("取消状态：", sum(1 for s in statuses if s == CANCELED), "个已取消，",
          sum(1 for s in statuses if s == OK), "个已完成")

    leftovers: list[str] = []
    for dirpath, _dirs, files in os.walk(OUTPUT):
        for name in files:
            if name.endswith(".part") or name.endswith(".tags.tmp") or name.startswith("~"):
                leftovers.append(os.path.join(dirpath, name))
    temp_dirs = [os.path.join(dp, d) for dp, dirs, _ in os.walk(OUTPUT) for d in dirs
                 if d.startswith(".ncm2mp3_tmp_")]

    print("残留半成品文件：", leftovers or "无")
    print("残留临时目录：", temp_dirs or "无")
    if os.path.isdir(OUTPUT):
        produce = [n for _, _, fs in os.walk(OUTPUT) for n in fs]
        print("目录内文件：", produce)

    ok = not leftovers and not temp_dirs and not thread.is_alive()
    print("结论：", "✔ 取消干净，无残留" if ok else "✘ 有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
