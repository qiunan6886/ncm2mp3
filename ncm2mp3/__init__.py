"""ncm2mp3 —— 把网易云 NCM 转成 mp3/flac 的小工具（纯标准库实现）。"""

from .ncm import NCMSong, NCMError, parse_ncm, decrypt_audio_to_file, decrypt_audio_to_bytes, aes_selftest

__all__ = [
    "NCMSong",
    "NCMError",
    "parse_ncm",
    "decrypt_audio_to_file",
    "decrypt_audio_to_bytes",
    "aes_selftest",
]
__version__ = "1.0.0"
