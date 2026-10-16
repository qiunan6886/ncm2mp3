"""校验已经转换好的音乐库文件：与官方 ncmdump.exe 的输出逐字节比对音频正文。

用法：
    python tools/verify_library.py <ncmdump.exe> <ncm 文件> <对应成品> ...
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crosscheck import digest_from, payload_offset  # noqa: E402


def check(exe: str, ncm: str, produced: str) -> bool:
    name = os.path.basename(produced)
    if not os.path.exists(ncm):
        print(f"✘ 源文件不存在：{ncm}")
        return False
    if not os.path.exists(produced):
        print(f"✘ 成品不存在：{produced}")
        return False
    fmt = "flac" if produced.lower().endswith(".flac") else "mp3"
    with tempfile.TemporaryDirectory(prefix="ncm2mp3_lib_") as workdir:
        subprocess.run([exe, "-o", workdir, ncm], capture_output=True)
        made = [os.path.join(workdir, n) for n in os.listdir(workdir)]
        if not made:
            print(f"✘ 官方 exe 没有产出：{ncm}")
            return False
        official = made[0]
        hash_official, size_official = digest_from(official, payload_offset(official, fmt))
        hash_produced, size_produced = digest_from(produced, payload_offset(produced, fmt))
    same = hash_official == hash_produced
    print(f"{'✔' if same else '✘'} {name}")
    print(f"    音频正文 {size_produced:,} B  sha256={hash_produced[:32]}…")
    if not same:
        print(f"    官方      {size_official:,} B  sha256={hash_official[:32]}…")
    return same


def main() -> int:
    if len(sys.argv) < 4 or (len(sys.argv) - 2) % 2:
        print(__doc__)
        return 2
    exe = sys.argv[1]
    pairs = sys.argv[2:]
    results = [check(exe, pairs[i], pairs[i + 1]) for i in range(0, len(pairs), 2)]
    ok = sum(1 for r in results if r)
    print(f"\n汇总：{ok}/{len(results)} 个成品与官方实现逐字节一致")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
