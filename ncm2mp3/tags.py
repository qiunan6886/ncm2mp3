"""标签与封面写入：MP3 用 ID3v2.3，FLAC 用 Vorbis Comment + PICTURE。

全部手写，不依赖 mutagen 之类的库。写入时先写临时文件再原子替换，
保证中途出错不会毁掉已解密好的音频。
"""

from __future__ import annotations

import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------- #
# 公共小工具
# --------------------------------------------------------------------------- #

def _syncsafe(n: int) -> bytes:
    """ID3 用的 28 位同步安全整数。"""
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _temp_beside(path: str) -> str:
    return path + ".tags.tmp"


def _replace_with_temp(temp: str, path: str) -> None:
    os.replace(temp, path)


def _discard(path: str) -> None:
    """清理写标签过程中可能留下的临时文件。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# ID3v2.3（MP3）
# --------------------------------------------------------------------------- #

def _id3_text_frame(frame_id: str, text: str) -> bytes:
    if not text:
        return b""
    if all(ord(ch) < 256 for ch in text):
        body = b"\x00" + text.encode("latin-1", "replace")
    else:
        body = b"\x01" + b"\xff\xfe" + text.encode("utf-16-le")
    return frame_id.encode("ascii") + struct.pack(">I", len(body)) + b"\x00\x00" + body


def _id3_apic_frame(mime: str, image: bytes) -> bytes:
    if not image:
        return b""
    body = (
        b"\x00"
        + mime.encode("ascii", "replace")
        + b"\x00"
        + b"\x03"                      # 图片类型：封面
        + b"\x00"                      # 描述（latin-1 空串）
        + image
    )
    return b"APIC" + struct.pack(">I", len(body)) + b"\x00\x00" + body


def build_id3v23_tag(info: Dict[str, str], cover: bytes = b"", cover_mime: str = "image/jpeg") -> bytes:
    """构造一个完整的 ID3v2.3 标签块。"""
    frames = b""
    frames += _id3_text_frame("TIT2", info.get("title", ""))
    frames += _id3_text_frame("TPE1", info.get("artist", ""))
    frames += _id3_text_frame("TPE2", info.get("album_artist", "") or info.get("artist", ""))
    frames += _id3_text_frame("TALB", info.get("album", ""))
    frames += _id3_text_frame("TRCK", info.get("track", ""))
    frames += _id3_text_frame("TYER", info.get("year", ""))
    frames += _id3_text_frame("TENC", info.get("encoder", ""))
    if cover:
        frames += _id3_apic_frame(cover_mime, cover)
    return b"ID3\x03\x00\x00" + _syncsafe(len(frames)) + frames


def _existing_id3_size(head: bytes) -> int:
    """若文件以 ID3v2 标签开头，返回整个标签长度，否则 0。"""
    if not head.startswith(b"ID3"):
        return 0
    size = 0
    for b in head[6:10]:
        size = (size << 7) | (b & 0x7F)
    total = size + 10
    if head[5] & 0x10:  # footer 标志
        total += 10
    return total


def write_id3v23(path: str, info: Dict[str, str], cover: bytes = b"", cover_mime: str = "image/jpeg") -> None:
    """把 ID3v2.3 标签写到 MP3 开头（替换已有标签）。"""
    tag = build_id3v23_tag(info, cover, cover_mime)
    temp = _temp_beside(path)
    try:
        with open(path, "rb") as src:
            head = src.read(10)
            old = _existing_id3_size(head)
            if old:
                src.seek(old)
            with open(temp, "wb") as dst:
                dst.write(tag)
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
        _replace_with_temp(temp, path)
    except BaseException:
        _discard(temp)
        raise


def id3_tag_size(head: bytes) -> int:
    """公开别名：判断文件头是否是 ID3v2 标签并返回其长度。"""
    return _existing_id3_size(head)


def read_id3v23(path: str) -> Dict[str, str]:
    """读回 ID3v2 文本帧（校验用，兼容 v2.3 与 v2.4 两种帧长度写法）。"""
    out: Dict[str, str] = {}
    with open(path, "rb") as f:
        head = f.read(10)
        if not head.startswith(b"ID3"):
            return out
        is_v24 = head[3] == 4
        size = 0
        for b in head[6:10]:
            size = (size << 7) | (b & 0x7F)
        data = f.read(size)
        pos = 0
        while pos + 10 <= len(data):
            fid = data[pos:pos + 4]
            if not fid.strip(b"\x00"):
                break
            if is_v24:
                flen = 0
                for b in data[pos + 4:pos + 8]:
                    flen = (flen << 7) | (b & 0x7F)
            else:
                flen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
            if flen <= 0 or pos + 10 + flen > len(data):
                break
            body = data[pos + 10:pos + 10 + flen]
            pos += 10 + flen
            if not fid.startswith(b"T") or not body:
                continue
            enc = body[0]
            raw = body[1:]
            try:
                if enc == 0:
                    text = raw.decode("latin-1")
                elif enc == 1:
                    text = raw.decode("utf-16")
                else:
                    text = raw.decode("utf-8")
            except Exception:
                continue
            out[fid.decode("ascii", "replace")] = text.strip("\x00")
    return out


# --------------------------------------------------------------------------- #
# FLAC（Vorbis Comment + PICTURE）
# --------------------------------------------------------------------------- #

def _vorbis_comment_block(fields: Sequence[Tuple[str, str]]) -> bytes:
    vendor = b"ncm2mp3"
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(fields))
    for key, value in fields:
        item = f"{key}={value}".encode("utf-8")
        out += struct.pack("<I", len(item)) + item
    return out


def _flac_picture_block(mime: str, image: bytes, desc: str = "") -> bytes:
    m = mime.encode("ascii", "replace")
    d = desc.encode("utf-8")
    return (
        struct.pack(">I", 3)                 # 图片类型：封面
        + struct.pack(">I", len(m)) + m
        + struct.pack(">I", len(d)) + d
        + struct.pack(">IIII", 0, 0, 0, 0)   # 宽/高/色深/调色板，0 表示未知
        + struct.pack(">I", len(image)) + image
    )


def _parse_flac_metadata(f) -> List[Tuple[int, bytes]]:
    """读取全部元数据块，文件指针停在音频数据起点。"""
    if f.read(4) != b"fLaC":
        raise ValueError("不是 FLAC 文件")
    blocks: List[Tuple[int, bytes]] = []
    while True:
        header = f.read(4)
        if len(header) < 4:
            raise ValueError("FLAC 元数据块不完整")
        is_last = bool(header[0] & 0x80)
        btype = header[0] & 0x7F
        length = int.from_bytes(header[1:4], "big")
        data = f.read(length)
        if len(data) != length:
            raise ValueError("FLAC 元数据块长度与内容不符")
        blocks.append((btype, data))
        if is_last:
            break
    return blocks


def write_flac_tags(path: str, info: Dict[str, str], cover: bytes = b"", cover_mime: str = "image/jpeg") -> None:
    """重写 FLAC 元数据区：保留 STREAMINFO 等原始块，替换标签与封面。"""
    temp = _temp_beside(path)
    try:
        with open(path, "rb") as src:
            blocks = _parse_flac_metadata(src)
            audio_start = src.tell()

            kept = [(t, d) for (t, d) in blocks if t not in (4, 6)]  # 去掉旧的 VORBIS_COMMENT / PICTURE
            if not kept or kept[0][0] != 0:
                raise ValueError("FLAC 缺少 STREAMINFO 块")

            fields: List[Tuple[str, str]] = []
            for key, field in (
                ("TITLE", "title"),
                ("ARTIST", "artist"),
                ("ALBUM", "album"),
                ("ALBUMARTIST", "album_artist"),
                ("TRACKNUMBER", "track"),
                ("DATE", "year"),
                ("ENCODER", "encoder"),
            ):
                value = info.get(field, "")
                if value:
                    fields.append((key, value))
            new_blocks: List[Tuple[int, bytes]] = list(kept)
            new_blocks.append((4, _vorbis_comment_block(fields)))
            if cover:
                new_blocks.append((6, _flac_picture_block(cover_mime, cover)))

            with open(temp, "wb") as dst:
                dst.write(b"fLaC")
                for index, (btype, data) in enumerate(new_blocks):
                    last = 0x80 if index == len(new_blocks) - 1 else 0x00
                    dst.write(bytes([last | btype]))
                    dst.write(len(data).to_bytes(3, "big"))
                    dst.write(data)
                src.seek(audio_start)
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
        _replace_with_temp(temp, path)
    except BaseException:
        _discard(temp)
        raise


def read_flac_tags(path: str) -> Dict[str, str]:
    """读回 FLAC 的 Vorbis Comment（校验用）。"""
    out: Dict[str, str] = {}
    with open(path, "rb") as f:
        for btype, data in _parse_flac_metadata(f):
            if btype != 4:
                continue
            pos = 0
            vlen = struct.unpack("<I", data[pos:pos + 4])[0]
            pos += 4 + vlen
            count = struct.unpack("<I", data[pos:pos + 4])[0]
            pos += 4
            for _ in range(count):
                if pos + 4 > len(data):
                    break
                clen = struct.unpack("<I", data[pos:pos + 4])[0]
                pos += 4
                item = data[pos:pos + clen].decode("utf-8", "replace")
                pos += clen
                if "=" in item:
                    key, value = item.split("=", 1)
                    out[key.upper()] = value
    return out


# --------------------------------------------------------------------------- #

def sanitize_filename(name: str, max_len: int = 150) -> str:
    """把歌曲名清理成合法文件名。"""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = name.strip().rstrip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"
