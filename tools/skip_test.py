"""「已经转换过的不再重复转换」测试。

覆盖三种情况：
1. 目标目录里已经有完全同名的成品 → 跳过；
2. 目标目录里只有「同名但后缀不同」的成品（例如之前转成过 mp3）→ 也要跳过；
3. 勾选「覆盖」时 → 不跳过，正常重新转换。

用法：
    python tools/skip_test.py <ncm 文件>
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.core import (  # noqa: E402
    OK, SKIPPED, ConvertOptions, Converter, expected_output_path, find_existing_audio, parse_ncm, scan,
)


def run(source: str, output: str, **kwargs):
    options = ConvertOptions(output_dir=output, overwrite=kwargs.pop("overwrite", False),
                             verify=False, jobs=1, **kwargs)
    return Converter(options).run(scan([source]))[0]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    source = sys.argv[1]
    song = parse_ncm(source)
    fmt = song.audio_format
    stem = os.path.splitext(os.path.basename(source))[0]
    failures = 0

    workdir = tempfile.mkdtemp(prefix="ncm2mp3_skip_")
    try:
        # 情况 1：同名成品已存在
        case1 = os.path.join(workdir, "case1")
        os.makedirs(case1, exist_ok=True)
        open(os.path.join(case1, f"{stem}.{fmt}"), "wb").write(b"fake-existing")
        result = run(source, case1)
        ok = result.status == SKIPPED
        failures += 0 if ok else 1
        print(f"{'✔' if ok else '✘'} 情况1 同名成品已存在 → {result.status}：{result.message}")
        print(f"      文件是否被改动：{'否' if open(os.path.join(case1, f'{stem}.{fmt}'), 'rb').read() == b'fake-existing' else '是'}")
        if not ok:
            failures += 1

        # 情况 2：只有同名但后缀不同的成品
        case2 = os.path.join(workdir, "case2")
        os.makedirs(case2, exist_ok=True)
        other_ext = ".mp3" if fmt != "mp3" else ".flac"
        open(os.path.join(case2, f"{stem}{other_ext}"), "wb").write(b"fake-other")
        result = run(source, case2)
        ok = result.status == SKIPPED
        failures += 0 if ok else 1
        print(f"{'✔' if ok else '✘'} 情况2 只有 {other_ext} 版本 → {result.status}：{result.message}")

        # 情况 3：勾选覆盖
        case3 = os.path.join(workdir, "case3")
        os.makedirs(case3, exist_ok=True)
        open(os.path.join(case3, f"{stem}.{fmt}"), "wb").write(b"fake-existing")
        result = run(source, case3, overwrite=True)
        real = os.path.getsize(os.path.join(case3, f"{stem}.{fmt}")) > 10000
        ok = result.status == OK and real
        failures += 0 if ok else 1
        print(f"{'✔' if ok else '✘'} 情况3 勾选覆盖 → {result.status}（产物 {os.path.getsize(result.dst):,} B）")

        # 情况 4：目录里没有成品
        case4 = os.path.join(workdir, "case4")
        result = run(source, case4)
        ok = result.status == OK
        failures += 0 if ok else 1
        print(f"{'✔' if ok else '✘'} 情况4 全新转换 → {result.status}（产物 {os.path.getsize(result.dst):,} B）")

        # 情况 5：expected_output_path / find_existing_audio 的一致性
        options = ConvertOptions(output_dir=case1)
        item = scan([source])[0]
        guess = expected_output_path(item, options)
        found = find_existing_audio(guess)
        ok = bool(found) and os.path.basename(found) == f"{stem}.{fmt}"
        failures += 0 if ok else 1
        print(f"{'✔' if ok else '✘'} 情况5 界面预判路径 → {os.path.basename(guess)}（命中 {os.path.basename(found or '无')}）")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("结论：", "✔ 全部符合预期" if failures == 0 else f"✘ {failures} 项不符合预期")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
