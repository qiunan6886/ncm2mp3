"""重复音乐检测与清理。

判定原则是「宁可不报，也不误删」：

1. **同名重复**：同一目录下，文件名去掉结尾的 `(1)`、`（2）`、`- 副本` 之类后相同，
   且**音频正文的 sha256 完全相同** → 确认重复，默认勾选；
2. **同名内容不同**：同上但音频内容不同（例如不同码率的重录）→ 只提示，默认不勾选；
3. **不同格式**：同一目录下同名但后缀不同（flac + mp3）→ 保留无损那份，默认不勾选；
4. **跨目录同名**：不同目录下的同名歌曲 → 只提示，默认不勾选。

删除一律走 **Windows 回收站**（可恢复），并且每一组至少保留一个文件。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import tags as tagmod
from .core import AUDIO_EXTENSIONS, flac_stream_info, human_size

# 格式优先级：数字越小越"值得保留"
FORMAT_RANK = {".flac": 0, ".ape": 1, ".wv": 1, ".wav": 1, ".m4a": 2, ".ogg": 2, ".mp3": 3, ".aac": 4}

CATEGORY_SAME_CONTENT = "同名重复（内容相同）"
CATEGORY_SAME_DIFF = "同名重复（内容不同）"
CATEGORY_FORMAT = "同曲不同格式"
CATEGORY_CROSS_DIR = "跨目录同名"

AUTO_CHECKED = {CATEGORY_SAME_CONTENT}

_SUFFIX_PATTERNS = (
    re.compile(r"[\s_\-]*[\(\（]\s*\d+\s*[\)\）]\s*$"),
    re.compile(r"[\s_\-]*(副本|拷贝|复制|copy)\s*$", re.IGNORECASE),
    re.compile(r"[\s_\-]*-\s*(副本|拷贝|复制)\s*$"),
)


def normalize_stem(stem: str) -> str:
    """把 `歌名 (1)`、`歌名（2）`、`歌名 - 副本` 归一成 `歌名`。"""
    original = stem.strip()
    text = original
    for _ in range(4):
        before = text
        for pattern in _SUFFIX_PATTERNS:
            text = pattern.sub("", text).strip()
        if text == before:
            break
    return (text or original).lower()


def audio_payload_digest(path: str, chunk: int = 1 << 20) -> str:
    """只对音频正文求 sha256（跳过标签/元数据），用来判断两首歌是不是同一段音频。"""
    ext = os.path.splitext(path)[1].lower()
    offset = 0
    try:
        if ext == ".mp3":
            with open(path, "rb") as handle:
                offset = tagmod.id3_tag_size(handle.read(10))
        elif ext == ".flac":
            offset = flac_stream_info(path)["audio_offset"]
    except Exception:
        offset = 0
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        handle.seek(offset)
        while True:
            block = handle.read(chunk)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class DupFile:
    path: str
    size: int = 0
    digest: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class DupGroup:
    category: str
    keep: DupFile
    remove: List[DupFile] = field(default_factory=list)
    note: str = ""

    @property
    def auto(self) -> bool:
        return self.category in AUTO_CHECKED

    @property
    def free_bytes(self) -> int:
        return sum(item.size for item in self.remove)

    def describe_remove(self, limit: int = 2) -> str:
        names = [item.name for item in self.remove]
        if len(names) <= limit:
            return "、".join(names)
        return "、".join(names[:limit]) + f" 等 {len(names)} 个"


def _keep_key(item: DupFile) -> Tuple:
    """排序键：优先保留无损、其次体积大、再次文件名短（没有 (1) 的那种）。"""
    return (FORMAT_RANK.get(item.ext, 9), -item.size, len(item.name), item.name)


def _collect_audio(root: str, on_progress: Optional[Callable[[int, int], None]] = None) -> List[DupFile]:
    found: List[DupFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                full = os.path.join(dirpath, name)
                try:
                    found.append(DupFile(path=full, size=os.path.getsize(full)))
                except OSError:
                    continue
    if on_progress:
        on_progress(len(found), len(found))
    return found


def scan_duplicates(root: str, on_progress: Optional[Callable[[int, int], None]] = None) -> List[DupGroup]:
    """扫描目录，返回所有重复分组（不删除任何文件）。"""
    root = os.path.abspath(root)
    files = _collect_audio(root, on_progress)
    groups: List[DupGroup] = []
    if not files:
        return groups

    same_dir: Dict[Tuple[str, str], List[DupFile]] = {}
    for item in files:
        stem = os.path.splitext(item.name)[0]
        key = (item.folder.lower(), normalize_stem(stem))
        same_dir.setdefault(key, []).append(item)

    handled: set = set()
    for items in same_dir.values():
        if len(items) < 2:
            continue
        for item in items:
            handled.add(os.path.normcase(item.path))
        by_ext: Dict[str, List[DupFile]] = {}
        for item in items:
            by_ext.setdefault(item.ext, []).append(item)

        # 同后缀：用音频正文哈希确认
        for ext, same_ext in by_ext.items():
            if len(same_ext) < 2:
                continue
            for item in same_ext:
                try:
                    item.digest = audio_payload_digest(item.path)
                except Exception:
                    item.digest = f"error:{item.path}"
            buckets: Dict[str, List[DupFile]] = {}
            for item in same_ext:
                buckets.setdefault(item.digest, []).append(item)
            for digest, bucket in buckets.items():
                if len(bucket) < 2:
                    continue
                keep = min(bucket, key=_keep_key)
                remove = [x for x in bucket if x is not keep]
                groups.append(DupGroup(CATEGORY_SAME_CONTENT, keep, remove,
                                       note=f"音频正文完全相同（{digest[:12]}…）"))

        # 同名但格式不同：保留无损
        if len(by_ext) > 1:
            keep = min(items, key=_keep_key)
            remove = [x for x in items if x is not keep]
            groups.append(DupGroup(CATEGORY_FORMAT, keep, remove,
                                   note="同名不同格式，默认保留音质最好的那份"))

        # 同后缀但内容彼此都不同：只提示，默认不勾选
        for ext, same_ext in by_ext.items():
            if len(same_ext) < 2:
                continue
            buckets: Dict[str, List[DupFile]] = {}
            for item in same_ext:
                buckets.setdefault(item.digest, []).append(item)
            leftovers = [item for bucket in buckets.values() if len(bucket) == 1 for item in bucket]
            if len(leftovers) >= 2:
                keep = min(leftovers, key=_keep_key)
                remove = [x for x in leftovers if x is not keep]
                groups.append(DupGroup(CATEGORY_SAME_DIFF, keep, remove,
                                       note="文件名相同但音频内容不同，请自行确认"))

    # 跨目录同名
    cross: Dict[str, List[DupFile]] = {}
    for item in files:
        stem = os.path.splitext(item.name)[0]
        cross.setdefault(normalize_stem(stem), []).append(item)
    for key, items in cross.items():
        folders = {item.folder.lower() for item in items}
        if len(items) < 2 or len(folders) < 2:
            continue
        pending = [x for x in items if os.path.normcase(x.path) not in handled]
        if len(pending) < 2:
            continue
        for item in pending:
            if not item.digest:
                try:
                    item.digest = audio_payload_digest(item.path)
                except Exception:
                    item.digest = f"error:{item.path}"
        keep = min(pending, key=_keep_key)
        remove = [x for x in pending if x is not keep]
        same_hash = len({x.digest for x in pending}) == 1
        groups.append(DupGroup(CATEGORY_CROSS_DIR, keep, remove,
                               note="不同目录下的同名歌曲"
                                    + ("，音频正文相同" if same_hash else "，音频正文不同")))

    groups.sort(key=lambda g: (-g.free_bytes, g.category, g.keep.name))
    return groups


# --------------------------------------------------------------------------- #
# 删除（走回收站）
# --------------------------------------------------------------------------- #

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


if os.name == "nt":
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]


def move_to_recycle_bin(paths: Sequence[str]) -> Tuple[int, List[str]]:
    """把文件送进 Windows 回收站，返回 (成功个数, 失败的路径列表)。

    非 Windows 或调用失败时不会退化成"直接删除"，而是原样报告失败。
    """
    targets = [os.path.abspath(p) for p in paths]
    missing = [p for p in targets if not os.path.exists(p)]
    existing = [p for p in targets if os.path.exists(p)]
    if not existing:
        return 0, missing
    if os.name != "nt":
        return 0, targets
    try:
        struct = _SHFILEOPSTRUCTW()
        struct.hwnd = None
        struct.wFunc = FO_DELETE
        struct.pFrom = "\0".join(existing) + "\0\0"
        struct.pTo = None
        struct.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        struct.fAnyOperationsAborted = False
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(struct))
    except Exception:
        return 0, targets
    if result != 0 or struct.fAnyOperationsAborted:
        return 0, targets
    still = [p for p in existing if os.path.exists(p)]
    return len(existing) - len(still), still + missing
