"""网易云音乐 NCM 文件解密核心。

NCM 文件结构（与 taurusxin/ncmdump 等开源实现一致）：

    "CTENFDAM"            8 字节魔数
    2 字节                 占位
    uint32 LE             密钥块长度
    密钥块                 逐字节 XOR 0x64 后做 AES-128-ECB 解密（末块去 PKCS#7
                          填充），去掉开头 17 字节 "neteasecloudmusic"，得到 RC4 密钥
    uint32 LE             元数据块长度
    元数据块               逐字节 XOR 0x63 后 base64 解码（跳过 22 字节前缀），
                          再做 AES-128-ECB 解密（末块去填充），去掉开头 6 字节
                          "music:"，得到 JSON
    uint32 LE             CRC32（不校验）
    1 字节                 图片版本
    uint32 LE             封面帧长度 cover_frame_len
    uint32 LE             封面图长度 image_size
    封面图                 JPEG/PNG 原始数据
    cover_frame_len - image_size 字节的补齐数据（通常为 0）
    剩余全部               音频数据，用由密钥生成的密钥流逐字节异或还原

注意：音频正文不做任何重编码，里面是什么格式（mp3/flac）解出来就是什么格式，
所以「转出来的音质」和网易云下载下来的一模一样。

音频本身不重新编码：原始数据是 mp3 就直接是 mp3，是 flac 就是 flac。
"""

from __future__ import annotations

import base64
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .aes import AES, pkcs7_unpad, selftest as _aes_selftest

MAGIC = b"CTENFDAM"
CORE_KEY = b"hzHRAmso5kInbaxW"
# 元数据 AES 密钥。历史上流传的写法有 15/16 字节两种，这里按优先级依次尝试，
# 以「能否解析出 JSON」为准自动选定。
META_KEYS = (
    bytes.fromhex("2331346C6A6B5F215C5D2630553C2728"),
    bytes.fromhex("2331346C6A6B5F215C5D2630553C27") + b"\x00",
)

READ_CHUNK = 1024 * 1024  # 1 MiB，且是 256 的整数倍，保证密钥流对齐

ProgressFn = Callable[[int, int], None]  # (已处理字节, 总字节)


class NCMError(Exception):
    """NCM 解析或解密失败。"""


# --------------------------------------------------------------------------- #
# 密钥盒 / 密钥流
# --------------------------------------------------------------------------- #

def aes_ecb_decrypt_unpad(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密，并按官方实现去掉最后一块的 PKCS#7 填充。

    这一步不能省：密钥块是 128 字节的加密数据，末块带着填充字节，
    官方实现（src/ncmcrypt.cpp 的 aesEcbDecrypt）会按最后一个字节指示的
    长度裁掉填充。若把填充字节也当成密钥的一部分，生成的密钥盒就完全不同，
    解出来的音频会是一片乱码。
    """
    aes = AES(key)
    if len(data) % 16:
        data = data[: len(data) - (len(data) % 16)]
    if not data:
        return b""
    plain = bytearray(aes.decrypt_ecb(data))
    pad = plain[-1]
    if pad > 16:
        pad = 0
    if pad:
        del plain[len(plain) - pad:]
    return bytes(plain)


def build_key_box(key: bytes) -> bytes:
    """由 RC4 密钥生成 256 字节密钥盒（标准 KSA）。"""
    box = bytearray(range(256))
    key_len = len(key)
    if key_len == 0:
        raise NCMError("NCM 密钥为空")
    c = 0
    last = 0
    off = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last + key[off]) & 0xFF
        off += 1
        if off >= key_len:
            off = 0
        box[i] = box[c]
        box[c] = swap
        last = c
    return bytes(box)


def build_keystream(key_box: bytes) -> bytes:
    """把 256 周期的密钥流展开成一张表，供分块异或使用。"""
    ks = bytearray(256)
    for i in range(256):
        j = (i + 1) & 0xFF
        ks[i] = key_box[(key_box[j] + key_box[(key_box[j] + j) & 0xFF]) & 0xFF]
    return bytes(ks)


def _xor_with_keystream(chunk: bytes, keystream: bytes) -> bytes:
    """按 256 周期异或；chunk 的起始位置必须是 256 的整数倍。"""
    if not chunk:
        return b""
    reps = (len(chunk) + 255) // 256
    pat = (keystream * reps)[: len(chunk)]
    return (int.from_bytes(chunk, "big") ^ int.from_bytes(pat, "big")).to_bytes(len(chunk), "big")


# --------------------------------------------------------------------------- #
# 结构解析
# --------------------------------------------------------------------------- #

@dataclass
class NCMSong:
    """一首 NCM 的全部解析结果。"""

    path: str
    size: int = 0
    audio_offset: int = 0
    audio_size: int = 0
    key_box: bytes = b""
    cover: bytes = b""
    meta: Dict = field(default_factory=dict)
    meta_key_index: int = -1
    raw_audio_magic: bytes = b""

    # -- 元数据便利属性 ---------------------------------------------------- #
    @property
    def music_name(self) -> str:
        return str(self.meta.get("musicName") or "")

    @property
    def album(self) -> str:
        return str(self.meta.get("album") or "")

    @property
    def artists(self) -> List[str]:
        out: List[str] = []
        for item in self.meta.get("artist") or []:
            if isinstance(item, (list, tuple)) and item:
                out.append(str(item[0]))
            elif isinstance(item, str):
                out.append(item)
        return out

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists)

    @property
    def bitrate(self) -> int:
        try:
            return int(self.meta.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def duration_ms(self) -> int:
        try:
            return int(self.meta.get("duration") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def declared_format(self) -> str:
        fmt = str(self.meta.get("format") or "").strip().lower()
        return fmt

    @property
    def audio_format(self) -> str:
        """按解密后音频的真实文件头判断格式，比元数据更可靠。"""
        head = self.raw_audio_magic
        if head.startswith(b"fLaC"):
            return "flac"
        if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            return "mp3"
        if head.startswith(b"OggS"):
            return "ogg"
        if head[4:8] == b"ftyp":
            return "m4a"
        if head.startswith(b"RIFF"):
            return "wav"
        return self.declared_format

    @property
    def cover_mime(self) -> str:
        c = self.cover
        if c.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if c.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if c.startswith(b"GIF8"):
            return "image/gif"
        if len(c) > 12 and c[4:12] == b"ftypheic":
            return "image/heic"
        return "image/jpeg"

    @property
    def cover_ext(self) -> str:
        return {"image/png": ".png", "image/gif": ".gif", "image/heic": ".heic"}.get(
            self.cover_mime, ".jpg"
        )

    def display_name(self) -> str:
        name = self.music_name or os.path.splitext(os.path.basename(self.path))[0]
        if self.artist_text:
            return f"{self.artist_text} - {name}"
        return name


def _read_exact(f, n: int, what: str) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise NCMError(f"文件在读取{what}时提前结束（期望 {n} 字节，实际 {len(data)} 字节）")
    return data


def _decrypt_meta(blob: bytes) -> Optional[Dict]:
    """元数据块可能用两种密钥之一，逐个尝试。"""
    if not blob:
        return None
    body = bytes(b ^ 0x63 for b in blob)
    if body.startswith(b"163 key(Don't modify):"):
        body = body[22:]
    try:
        body = base64.b64decode(body)
    except Exception:
        return None
    if len(body) % 16:
        body = body[: len(body) - (len(body) % 16)]
    if not body:
        return None
    for key in META_KEYS:
        try:
            plain = aes_ecb_decrypt_unpad(body, key)
        except Exception:
            continue
        if plain.startswith(b"music:"):
            plain = plain[6:]
        candidates = [plain, pkcs7_unpad(plain)]
        end = plain.rfind(b"}")
        if end != -1:
            candidates.append(plain[: end + 1])
        for cand in candidates:
            try:
                text = cand.decode("utf-8", "ignore").strip().strip("\x00")
                if not text.startswith("{"):
                    continue
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return None


def parse_ncm(path: str) -> NCMSong:
    """解析 NCM 头部，得到元数据、封面与音频起始偏移（不解密音频正文）。"""
    path = os.path.abspath(path)
    song = NCMSong(path=path)
    song.size = os.path.getsize(path)
    with open(path, "rb") as f:
        if _read_exact(f, 8, "魔数") != MAGIC:
            raise NCMError("不是有效的 NCM 文件（缺少 CTENFDAM 魔数）")
        f.read(2)
        key_len = struct.unpack("<I", _read_exact(f, 4, "密钥长度"))[0]
        if key_len == 0 or key_len > 1 << 20:
            raise NCMError(f"密钥块长度异常：{key_len}")
        key_blob = bytearray(_read_exact(f, key_len, "密钥块"))
        for i in range(len(key_blob)):
            key_blob[i] ^= 0x64
        key_data = aes_ecb_decrypt_unpad(bytes(key_blob), CORE_KEY)
        if not key_data.startswith(b"neteasecloudmusic"):
            # 兜底：个别文件末块没有填充，按未去填充的结果再试一次
            aligned = bytes(key_blob[: len(key_blob) - (len(key_blob) % 16)])
            key_data = AES(CORE_KEY).decrypt_ecb(aligned)
        if not key_data.startswith(b"neteasecloudmusic"):
            raise NCMError("核心密钥解密结果异常，文件可能不是标准 NCM")
        song.key_box = build_key_box(key_data[17:])

        meta_len = struct.unpack("<I", _read_exact(f, 4, "元数据长度"))[0]
        if meta_len:
            if meta_len > 1 << 24:
                raise NCMError(f"元数据块长度异常：{meta_len}")
            meta_blob = _read_exact(f, meta_len, "元数据块")
            song.meta = _decrypt_meta(meta_blob) or {}
        else:
            song.meta = {}

        f.read(4)                                        # CRC32，不校验
        f.read(1)                                        # 图片版本字节
        cover_frame_len = struct.unpack("<I", _read_exact(f, 4, "封面帧长度"))[0]
        image_size = struct.unpack("<I", _read_exact(f, 4, "封面长度"))[0]
        if image_size:
            if image_size > 64 << 20:
                raise NCMError(f"封面数据长度异常：{image_size}")
            song.cover = _read_exact(f, image_size, "封面数据")
        extra = cover_frame_len - image_size
        if 0 < extra < (64 << 20):
            f.seek(extra, os.SEEK_CUR)                   # 官方实现会跳过多出来的补齐数据
        song.audio_offset = f.tell()
        song.audio_size = max(0, song.size - song.audio_offset)
        head = f.read(16)
        if head:
            song.raw_audio_magic = _xor_with_keystream(head, build_keystream(song.key_box))[:16]
    return song


# --------------------------------------------------------------------------- #
# 音频解密
# --------------------------------------------------------------------------- #

def decrypt_audio_to_file(
    song: NCMSong,
    dst_path: str,
    progress: Optional[ProgressFn] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """把解密后的音频正文写入 dst_path，返回写入字节数。

    ``should_stop`` 返回 True 时提前结束（调用方负责删除半成品文件）。
    """
    keystream = build_keystream(song.key_box)
    total = song.audio_size
    done = 0
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)) or ".", exist_ok=True)
    with open(song.path, "rb") as src, open(dst_path, "wb") as dst:
        src.seek(song.audio_offset)
        while True:
            if should_stop and should_stop():
                break
            chunk = src.read(READ_CHUNK)
            if not chunk:
                break
            dst.write(_xor_with_keystream(chunk, keystream))
            done += len(chunk)
            if progress:
                progress(done, total)
    return done


def decrypt_audio_to_bytes(song: NCMSong) -> bytes:
    """小文件用：直接在内存里返回音频正文。"""
    with open(song.path, "rb") as f:
        f.seek(song.audio_offset)
        data = f.read()
    return _xor_with_keystream(data, build_keystream(song.key_box))


def aes_selftest() -> bool:
    return _aes_selftest()
