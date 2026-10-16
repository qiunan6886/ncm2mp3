"""深入排查：对比官方输出与本项目输出，定位差异到底在标签区还是音频正文。

用法：
    python tools/investigate.py <ncmdump.exe> <file.ncm>
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.ncm import decrypt_audio_to_file, parse_ncm  # noqa: E402


def show_id3(path: str) -> int:
    with open(path, "rb") as handle:
        head = handle.read(16)
        print(f"  头部字节      : {head[:10].hex(' ')}")
        if not head.startswith(b"ID3"):
            print("  没有 ID3v2 标签")
            return 0
        version = f"2.{head[3]}.{head[4]}"
        flags = head[5]
        size = 0
        for byte in head[6:10]:
            size = (size << 7) | (byte & 0x7F)
        print(f"  ID3 版本      : {version}  标志=0x{flags:02x}  声明长度={size:,}  含头总长={size + 10:,}")
        handle.seek(10)
        blob = handle.read(size)
        pos = 0
        count = 0
        while pos + 10 <= len(blob) and count < 40:
            fid = blob[pos:pos + 4]
            if not fid.strip(b"\x00"):
                break
            if version == "2.4":
                flen = 0
                for byte in blob[pos + 4:pos + 8]:
                    flen = (flen << 7) | (byte & 0x7F)
            else:
                flen = struct.unpack(">I", blob[pos + 4:pos + 8])[0]
            print(f"    帧 {fid.decode('ascii', 'replace')}  长度={flen:,}")
            count += 1
            if flen <= 0:
                break
            pos += 10 + flen
        return size + 10


def show_flac(path: str) -> int:
    with open(path, "rb") as handle:
        magic = handle.read(4)
        print(f"  头部字节      : {magic!r}")
        if magic != b"fLaC":
            return 0
        total = 0
        while True:
            header = handle.read(4)
            if len(header) < 4:
                return 0
            is_last = bool(header[0] & 0x80)
            btype = header[0] & 0x7F
            length = int.from_bytes(header[1:4], "big")
            names = {0: "STREAMINFO", 1: "PADDING", 2: "APPLICATION", 3: "SEEKTABLE",
                     4: "VORBIS_COMMENT", 5: "CUESHEET", 6: "PICTURE"}
            print(f"    块 {names.get(btype, btype)}  长度={length:,}{'  (最后一块)' if is_last else ''}")
            handle.seek(length, os.SEEK_CUR)
            total += 4 + length
            if is_last:
                return 4 + total


def tail_check(path: str) -> None:
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        handle.seek(max(0, size - 128))
        tail = handle.read(128)
    print(f"  文件尾部      : {'ID3v1 标签(128B)' if tail[:3] == b'TAG' else tail[:8].hex(' ')}")


def locate(payload_path: str, container_path: str) -> None:
    with open(payload_path, "rb") as handle:
        needle = handle.read(64)
        payload_size = os.path.getsize(payload_path)
    with open(container_path, "rb") as handle:
        window = handle.read(4 << 20)
    index = window.find(needle)
    print(f"  原始负载      : {payload_size:,} B  签名={needle[:8].hex(' ')}")
    print(f"  在官方文件中定位签名 : {'偏移 ' + format(index, ',') if index >= 0 else '未找到'}")
    if index < 0:
        return
    with open(container_path, "rb") as handle:
        handle.seek(index)
        same = True
        compared = 0
        while True:
            a = handle.read(1 << 20)
            if not a:
                break
            with open(payload_path, "rb") as ph:
                ph.seek(compared)
                b = ph.read(len(a))
            if a != b:
                for i in range(min(len(a), len(b))):
                    if a[i] != b[i]:
                        print(f"  ✘ 第 {compared + i:,} 字节起不一致: 官方={a[i]:02x} 本项目={b[i]:02x}")
                        break
                same = False
                break
            compared += len(a)
    print(f"  官方文件尾部剩余      : {os.path.getsize(container_path) - index - compared:,} B")
    print(f"  音频正文比对          : {'✔ 完全相同' if same and compared == payload_size else '✘ 有差异'}")


def main() -> int:
    exe, ncm_path = sys.argv[1], sys.argv[2]
    print(f"### {os.path.basename(ncm_path)}")
    song = parse_ncm(ncm_path)
    fmt = song.audio_format
    workdir = tempfile.mkdtemp(prefix="ncm2mp3_investigate_")
    try:
        ours = os.path.join(workdir, f"ours.{fmt}")
        decrypt_audio_to_file(song, ours)
        offdir = os.path.join(workdir, "official")
        os.makedirs(offdir, exist_ok=True)
        subprocess.run([exe, "-o", offdir, ncm_path], capture_output=True)
        official = None
        for name in os.listdir(offdir):
            official = os.path.join(offdir, name)
        print(f"  本项目输出    : {os.path.getsize(ours):,} B")
        print(f"  官方输出      : {os.path.getsize(official):,} B")
        print("  --- 官方输出结构 ---")
        if fmt == "mp3":
            show_id3(official)
        else:
            show_flac(official)
        tail_check(official)
        print("  --- 正文比对 ---")
        locate(ours, official)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
