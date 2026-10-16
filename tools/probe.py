"""诊断脚本：扫描目录里的 ncm，报告真实音频格式/元数据/封面，不做任何转换。

用法：
    python tools/probe.py "G:\\Music\\网易云"
"""

from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.ncm import NCMError, parse_ncm  # noqa: E402


def find_ncm(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(".ncm"):
                yield os.path.join(dirpath, name)


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(find_ncm(root))
    print(f"扫描目录: {os.path.abspath(root)}")
    print(f"发现 ncm: {len(files)}\n")

    fmt_counter: Counter = Counter()
    bitrate_counter: Counter = Counter()
    errors = []
    total_bytes = 0

    name_w = 46
    print(f"{'格式':<6}{'码率':>7}  {'时长':>7}  {'封面':>8}  {'大小':>10}  歌曲")
    print("-" * 110)
    for path in files:
        try:
            song = parse_ncm(path)
        except NCMError as exc:
            errors.append((path, str(exc)))
            print(f"{'!!':<6}{'':>7}  {'':>7}  {'':>8}  {os.path.getsize(path):>10,}  {os.path.basename(path)}  <- {exc}")
            continue
        fmt = song.audio_format
        fmt_counter[fmt] += 1
        bitrate_counter[song.bitrate] += 1
        total_bytes += song.audio_size
        dur = song.duration_ms / 1000 if song.duration_ms else 0
        label = song.display_name()[:name_w]
        print(
            f"{fmt:<6}{song.bitrate:>7}  {dur:>6.1f}s  {len(song.cover):>8,}  {song.audio_size:>10,}  {label}"
        )

    print("-" * 110)
    print(f"格式分布      : {dict(fmt_counter)}")
    print(f"码率分布(kbps): {dict(bitrate_counter)}")
    print(f"音频总大小    : {total_bytes / 1024 / 1024:.1f} MiB")
    if errors:
        print(f"失败 {len(errors)} 个：")
        for path, msg in errors:
            print(f"  - {path}: {msg}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
