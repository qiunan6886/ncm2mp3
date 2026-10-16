"""反推官方实现的密钥流，定位本项目音频解密的问题。

原理：官方输出（去掉标签区）与 ncm 里的原始密文逐字节异或，
就得到官方实际使用的密钥流。把它和本项目生成的密钥流对比，
即可判断是「密钥盒算错」还是「音频起始偏移算错」。
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.ncm import build_keystream, parse_ncm  # noqa: E402


def official_audio_offset(path: str) -> int:
    with open(path, "rb") as handle:
        head = handle.read(10)
        if head.startswith(b"ID3"):
            size = 0
            for byte in head[6:10]:
                size = (size << 7) | (byte & 0x7F)
            return size + 10
        if head[:4] == b"fLaC":
            handle.seek(4)
            while True:
                block = handle.read(4)
                if len(block) < 4:
                    return 0
                last = bool(block[0] & 0x80)
                length = int.from_bytes(block[1:4], "big")
                handle.seek(length, os.SEEK_CUR)
                if last:
                    return handle.tell()
    return 0


def main() -> int:
    exe, ncm_path = sys.argv[1], sys.argv[2]
    song = parse_ncm(ncm_path)
    ours_ks = build_keystream(song.key_box)
    print(f"### {os.path.basename(ncm_path)}")
    print(f"  本项目判定格式      : {song.declared_format!r} (元数据)")
    print(f"  本项目读到音频偏移  : {song.audio_offset:,}  音频大小 {song.audio_size:,}")
    print(f"  本项目 raw_audio_magic: {song.raw_audio_magic.hex(' ')}")

    workdir = tempfile.mkdtemp(prefix="ncm2mp3_ks_")
    try:
        offdir = os.path.join(workdir, "official")
        os.makedirs(offdir, exist_ok=True)
        subprocess.run([exe, "-o", offdir, ncm_path], capture_output=True)
        official = os.path.join(offdir, os.listdir(offdir)[0])
        off = official_audio_offset(official)
        print(f"  官方输出            : {os.path.getsize(official):,} B  音频起始 {off:,}")

        with open(official, "rb") as fo, open(ncm_path, "rb") as fn:
            fo.seek(off)
            fn.seek(song.audio_offset)
            official_audio = fo.read(1024)
            cipher = fn.read(1024)
        print(f"  官方音频头 16 字节  : {official_audio[:16].hex(' ')}")
        print(f"  ncm 原始密文 16 字节: {cipher[:16].hex(' ')}")

        recovered = bytes(a ^ b for a, b in zip(official_audio, cipher))
        print(f"  反推密钥流 32 字节  : {recovered[:32].hex(' ')}")
        print(f"  本项目密钥流 32 字节: {ours_ks[:32].hex(' ')}")

        print("  --- 密钥流对齐检查 ---")
        matched = None
        for shift in range(256):
            if all(recovered[i] == ours_ks[(i + shift) % 256] for i in range(256)):
                matched = shift
                break
        if matched is None:
            print("  ✘ 与本项目密钥流在任意 256 周期移位下都不匹配 → 密钥盒/密钥块有问题")
            common = sum(1 for i in range(256) if recovered[i] == ours_ks[i])
            print(f"    同位置相同字节数：{common}/256")
        else:
            print(f"  ✔ 密钥流一致，官方流相对本项目有 {matched} 字节移位 → 音频起始偏移差 {matched} 字节")

        print("  --- 官方输出音频区尾部 ---")
        size = os.path.getsize(official)
        with open(official, "rb") as fo:
            fo.seek(max(off, size - 160))
            print(f"    末尾: {fo.read(160)[:32].hex(' ')}")
        with open(ncm_path, "rb") as fn:
            fn.seek(max(song.audio_offset, song.size - 160))
            print(f"    密文尾: {fn.read(160)[:32].hex(' ')}")
        print(f"    官方音频区长度 = {size - off:,}")
        print(f"    本项目音频长度 = {song.audio_size:,}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
