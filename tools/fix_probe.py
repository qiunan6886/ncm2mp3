"""定位实验：分别验证「密钥是否去填充」和「音频起始偏移」两个变量。

把 2 种密钥处理 × 2 种偏移组合起来，用官方 exe 的输出作为标准答案，
看哪一种组合能解出正确的音频头（并且与官方输出一致）。
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.aes import AES                                   # noqa: E402
from ncm2mp3.ncm import CORE_KEY, MAGIC, build_key_box, build_keystream  # noqa: E402


def u32(data: bytes) -> int:
    return struct.unpack("<I", data)[0]


def decrypt_head(path: str, key_box: bytes, offset: int, length: int = 32) -> bytes:
    keystream = build_keystream(key_box)
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    reps = (len(data) + 255) // 256
    pattern = (keystream * reps)[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pattern, "big")).to_bytes(len(data), "big")


def official_audio(path: str) -> bytes:
    with open(path, "rb") as handle:
        head = handle.read(10)
        if head.startswith(b"ID3"):
            size = 0
            for byte in head[6:10]:
                size = (size << 7) | (byte & 0x7F)
            handle.seek(size + 10)
        elif head[:4] == b"fLaC":
            handle.seek(4)
            while True:
                block = handle.read(4)
                if len(block) < 4:
                    break
                last = bool(block[0] & 0x80)
                handle.seek(int.from_bytes(block[1:4], "big"), os.SEEK_CUR)
                if last:
                    break
        else:
            return b""
        return handle.read(32)


def main() -> int:
    exe, ncm_path = sys.argv[1], sys.argv[2]
    print(f"### {os.path.basename(ncm_path)}")
    with open(ncm_path, "rb") as handle:
        if handle.read(8) != MAGIC:
            print("不是 ncm")
            return 1
        handle.read(2)
        key_len = u32(handle.read(4))
        key_blob = bytearray(handle.read(key_len))
        for i in range(len(key_blob)):
            key_blob[i] ^= 0x64
        if len(key_blob) % 16:
            key_blob = key_blob[: len(key_blob) - (len(key_blob) % 16)]
        key_plain = AES(CORE_KEY).decrypt_ecb(bytes(key_blob))
        meta_len = u32(handle.read(4))
        handle.read(meta_len)
        meta_end = handle.tell()
        crc = handle.read(4)
        version_byte = handle.read(1)
        cover_frame_len = u32(handle.read(4))
        image_size = u32(handle.read(4))
        image_start = handle.tell()

    pad = key_plain[-1]
    print(f"  密钥块长度        : {key_len}  →  解密后 {len(key_plain)}")
    print(f"  密钥明文首 17 字节: {key_plain[:17]!r}")
    print(f"  最后一块末字节    : 0x{pad:02x}（若 ≤16 则是 PKCS#7 填充长度）")
    print(f"  元数据结束位置    : {meta_end:,}")
    print(f"  CRC32             : {crc.hex(' ')}   版本字节=0x{version_byte[0]:02x}")
    print(f"  cover_frame_len   : {cover_frame_len:,}")
    print(f"  image_size (n)    : {image_size:,}")
    print(f"  cover_frame_len-n : {cover_frame_len - image_size:,}")
    print(f"  图片数据起始      : {image_start:,}")

    key_variants = {"不去填充": key_plain[17:]}
    if 1 <= pad <= 16:
        key_variants["去填充"] = key_plain[: len(key_plain) - pad][17:]
    else:
        print("  （末字节 >16，无需去填充，两种处理等价）")
        key_variants["去填充"] = key_plain[17:]

    offset_variants = {
        "旧偏移=13+n": image_start + image_size,
        "官方偏移=13+cover_frame_len": image_start + cover_frame_len,
    }

    workdir = tempfile.mkdtemp(prefix="ncm2mp3_fix_")
    try:
        offdir = os.path.join(workdir, "official")
        os.makedirs(offdir, exist_ok=True)
        subprocess.run([exe, "-o", offdir, ncm_path], capture_output=True)
        official_path = os.path.join(offdir, os.listdir(offdir)[0])
        reference = official_audio(official_path)
        print(f"  官方音频头 32 字节: {reference.hex(' ')}")
        print("  --- 组合判定 ---")
        best = None
        for key_name, key in key_variants.items():
            box = build_key_box(key)
            for off_name, offset in offset_variants.items():
                head = decrypt_head(ncm_path, box, offset)
                ok = head == reference
                kind = "?"
                if head[:4] == b"fLaC":
                    kind = "FLAC"
                elif head[:3] == b"ID3" or (head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
                    kind = "MP3"
                print(f"  {key_name:<8} {off_name:<26} → {head[:12].hex(' ')}  [{kind}]"
                      f"{'  ✔ 与官方一致' if ok else ''}")
                if ok:
                    best = (key_name, off_name, offset)
        if best:
            print(f"  ✔ 正确组合：密钥「{best[0]}」，偏移「{best[1]}」= {best[2]:,}")
        else:
            print("  ✘ 没有组合与官方一致，需要进一步排查")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
