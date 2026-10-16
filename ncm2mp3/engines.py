"""转换引擎。

本项目提供两种等价引擎：

* 内置引擎（默认）：纯 Python 实现，直接解读 ncm 结构。支持细粒度进度、
  随时取消、转换后自检；输出已与官方实现做过逐字节比对（见 tools/crosscheck.py）。
* 官方引擎：直接加载上游项目 taurusxin/ncmdump 发布的 libncmdump.dll，
  用 ctypes 调用它的 C 接口（CreateNeteaseCrypt / Dump / FixMetadata /
  DestroyNeteaseCrypt）。写标签与封面由官方代码（taglib）完成。

两种引擎输出的音频正文逐字节相同，区别只在标签写法、命名与进度反馈。
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import List, Optional

DLL_NAMES = ("libncmdump.dll", "ncmdump.dll")

OFFICIAL_REPO = "https://github.com/taurusxin/ncmdump"
OFFICIAL_DLL_URL = (
    "https://github.com/taurusxin/ncmdump/releases/download/1.5.1/"
    "libncmdump-1.5.1-windows-amd64.zip"
)


def candidate_dll_paths() -> List[str]:
    """按优先级列出可能放着 dll 的位置。"""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    roots = [
        # 打包成 exe 运行时，dll 可能被放在 exe 同级的 vendor 目录或临时解包目录
        getattr(sys, "_MEIPASS", "") or "",
        os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else "",
        os.path.dirname(package_dir),
        os.path.join(os.path.dirname(package_dir), "vendor", "libncmdump"),
        os.path.join(os.path.dirname(package_dir), "vendor"),
        package_dir,
        os.getcwd(),
    ]
    paths: List[str] = []
    for root in roots:
        if not root:
            continue
        for name in DLL_NAMES:
            paths.append(os.path.join(root, name))
    # exe 同级目录下的 vendor\libncmdump
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        for name in DLL_NAMES:
            paths.append(os.path.join(exe_dir, "vendor", "libncmdump", name))
            paths.append(os.path.join(exe_dir, "vendor", name))
    return paths


def find_official_dll() -> Optional[str]:
    for path in candidate_dll_paths():
        if os.path.isfile(path):
            return path
    return None


class EngineError(Exception):
    """引擎调用失败。"""


class OfficialEngine:
    """加载官方 libncmdump.dll 进行转换。"""

    kind = "official"
    label = "官方引擎（libncmdump.dll）"

    def __init__(self, dll_path: Optional[str] = None):
        self.dll_path = dll_path or find_official_dll()
        if not self.dll_path or not os.path.isfile(self.dll_path):
            raise EngineError("未找到 libncmdump.dll")
        try:
            self._dll = ctypes.WinDLL(self.dll_path)
        except OSError as exc:
            raise EngineError(f"加载 {self.dll_path} 失败：{exc}") from exc
        try:
            self._dll.CreateNeteaseCrypt.argtypes = [ctypes.c_char_p]
            self._dll.CreateNeteaseCrypt.restype = ctypes.c_void_p
            self._dll.Dump.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            self._dll.Dump.restype = ctypes.c_int
            self._dll.FixMetadata.argtypes = [ctypes.c_void_p]
            self._dll.FixMetadata.restype = None
            self._dll.DestroyNeteaseCrypt.argtypes = [ctypes.c_void_p]
            self._dll.DestroyNeteaseCrypt.restype = None
        except AttributeError as exc:
            raise EngineError(f"{self.dll_path} 的接口不符合预期：{exc}") from exc

    # ------------------------------------------------------------------ #
    def dump(self, ncm_path: str, out_dir: str, write_tags: bool = True) -> str:
        """把 ncm 导出到 out_dir，返回生成的文件完整路径。

        官方库按元数据决定文件名，所以调用方通常把 out_dir 指向临时目录，
        拿到文件后再改名成自己想要的名字。
        """
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        before = set(os.listdir(out_dir))
        handle = self._dll.CreateNeteaseCrypt(os.path.abspath(ncm_path).encode("utf-8"))
        if not handle:
            raise EngineError("官方库无法打开该文件（可能不是有效的 NCM）")
        try:
            if self._dll.Dump(handle, os.path.abspath(out_dir).encode("utf-8")) != 0:
                raise EngineError("官方库导出失败")
            if write_tags:
                self._dll.FixMetadata(handle)
        finally:
            try:
                self._dll.DestroyNeteaseCrypt(handle)
            except Exception:
                pass
        created = [name for name in os.listdir(out_dir) if name not in before]
        if not created:
            raise EngineError("官方库没有生成输出文件")
        return os.path.join(out_dir, created[0])


def engine_choices() -> List[tuple]:
    """给界面用的引擎下拉项：(值, 显示文字, 是否可用)。"""
    has_dll = find_official_dll() is not None
    return [
        ("builtin", "内置引擎（推荐：有进度、可取消、有自检）", True),
        ("official", "官方引擎（libncmdump.dll）", has_dll),
    ]
