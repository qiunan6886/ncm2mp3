"""交叉验证：把本项目的解密结果和官方 ncmdump.exe 的输出做逐字节比对。

官方 exe 会在音频前面写标签（MP3 的 ID3v2 / FLAC 的元数据块），
所以比对时要把这部分剥掉，只比较音频正文。

用法：
    python tools/crosscheck.py <ncmdump.exe> <file1.ncm> [file2.ncm ...]
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.ncm import decrypt_audio_to_file, parse_ncm  # noqa: E402
from ncm2mp3.tags import id3_tag_size                      # noqa: E402


def flac_audio_offset(path: str) -> int:
    with open(path, "rb") as handle:
        if handle.read(4) != b"fLaC":
            return 0
        while True:
            header = handle.read(4)
            if len(header) < 4:
                return 0
            is_last = bool(header[0] & 0x80)
            handle.seek(int.from_bytes(header[1:4], "big"), os.SEEK_CUR)
            if is_last:
                return handle.tell()


def payload_offset(path: str, fmt: str) -> int:
    if fmt == "mp3":
        with open(path, "rb") as handle:
            return id3_tag_size(handle.read(10))
    if fmt == "flac":
        return flac_audio_offset(path)
    return 0


def digest_from(path: str, offset: int):
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def find_output(folder: str) -> str:
    best = None
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if os.path.isfile(full) and name.lower().endswith((".mp3", ".flac", ".ogg", ".m4a", ".wav")):
            best = full
    return best


def check(exe: str, ncm_path: str) -> bool:
    name = os.path.basename(ncm_path)
    print(f"\n=== {name} ===")
    song = parse_ncm(ncm_path)
    fmt = song.audio_format
    print(f"  本项目识别格式 : {fmt}  时长 {song.duration_ms / 1000:.1f}s  封面 {len(song.cover)}B")

    workdir = tempfile.mkdtemp(prefix="ncm2mp3_cross_")
    try:
        ours = os.path.join(workdir, f"ours.{fmt}")
        ours_size = decrypt_audio_to_file(song, ours)

        official_dir = os.path.join(workdir, "official")
        os.makedirs(official_dir, exist_ok=True)
        proc = subprocess.run(
            [exe, "-o", official_dir, ncm_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        official = find_output(official_dir)
        if not official:
            print(f"  ✘ 官方 exe 没有产生输出 (exit={proc.returncode})")
            print(f"    stdout: {proc.stdout.strip()[:300]}")
            print(f"    stderr: {proc.stderr.strip()[:300]}")
            return False

        official_size = os.path.getsize(official)
        off = payload_offset(official, fmt)
        off_ours = payload_offset(ours, fmt)
        print(f"  官方输出       : {os.path.basename(official)}  {official_size:,} B（标签/元数据 {off:,} B）")
        print(f"  本项目输出     : {os.path.basename(ours)}  {os.path.getsize(ours):,} B（文件头 {off_ours:,} B）")

        hash_official, size_official = digest_from(official, off)
        hash_ours, size_ours = digest_from(ours, off_ours)
        same = hash_official == hash_ours and size_official == size_ours
        print(f"  官方音频正文   : {size_official:,} B  sha256={hash_official[:24]}…")
        print(f"  本项目音频正文 : {size_ours:,} B  sha256={hash_ours[:24]}…")
        print(f"  结论           : {'✔ 完全一致（逐字节相同）' if same else '✘ 不一致'}")
        return same
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    exe = sys.argv[1]
    results = [check(exe, path) for path in sys.argv[2:]]
    ok = sum(1 for r in results if r)
    print(f"\n汇总：{ok}/{len(results)} 个文件与官方实现完全一致")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
