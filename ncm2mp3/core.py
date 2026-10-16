"""转换核心：扫描目录、解密、写标签、校验、批量调度。

设计要点：
* 不做任何重新编码 —— mp3 出来还是 mp3，flac 出来还是 flac，音质保持原样；
* 只用标准库，用户机器不需要安装 Python 包、也不需要 ffmpeg；
* 转换在后台线程池里跑，通过回调向界面汇报进度，可随时取消。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from . import tags as tagmod
from .ncm import NCMSong, decrypt_audio_to_file, parse_ncm

# --------------------------------------------------------------------------- #
# 状态常量
# --------------------------------------------------------------------------- #

PENDING = "pending"
RUNNING = "running"
OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"
CANCELED = "canceled"

NCM_EXT = ".ncm"
ENCODER_TAG = "ncm2mp3"

ProgressFn = Callable[[float], None]           # 单个文件进度 0.0 ~ 1.0
LogFn = Callable[[str], None]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def human_size(num: int) -> str:
    if not num or num <= 0:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} B"


def human_duration(ms: int) -> str:
    if not ms or ms <= 0:
        return "-"
    total = int(ms / 1000)
    return f"{total // 60}:{total % 60:02d}"


def describe_format(fmt: str) -> str:
    return {"mp3": "MP3", "flac": "FLAC", "ogg": "OGG", "m4a": "M4A", "wav": "WAV"}.get(
        (fmt or "").lower(), (fmt or "未知").upper()
    )


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class SourceItem:
    """一个待转换的 ncm 文件（含解析出来的元数据）。"""

    path: str
    rel_dir: str = ""
    size: int = 0
    audio_size: int = 0
    fmt: str = ""
    duration_ms: int = 0
    bitrate: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_size: int = 0
    error: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def song_display(self) -> str:
        if self.title:
            return f"{self.artist} - {self.title}" if self.artist else self.title
        return self.stem

    @property
    def fmt_text(self) -> str:
        if self.error:
            return "无法解析"
        return describe_format(self.fmt)

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def duration_text(self) -> str:
        return human_duration(self.duration_ms)


@dataclass
class ConvertOptions:
    """转换选项。默认值就是界面上的默认值。"""

    output_dir: Optional[str] = None     # None = 输出到源文件所在目录
    keep_structure: bool = True          # 指定输出目录时保留子目录结构
    use_song_name: bool = False          # 用「歌手 - 歌名」命名输出文件
    overwrite: bool = False              # 覆盖已存在的同名文件
    delete_source: bool = False          # 转换成功后删除原 ncm
    write_tags: bool = True              # 写入标题/歌手/专辑/封面
    save_cover: bool = False             # 额外导出封面图片
    verify: bool = True                  # 转换后校验输出文件
    jobs: int = 4                        # 并发线程数
    engine: str = "builtin"              # builtin=内置引擎，official=官方 libncmdump.dll
    artist_subfolder: bool = False       # 源目录直属的文件，按歌手归入子目录


@dataclass
class FileResult:
    src: str
    dst: str = ""
    fmt: str = ""
    status: str = PENDING
    message: str = ""
    seconds: float = 0.0
    out_size: int = 0
    title: str = ""
    artist: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #

def scan(paths: Sequence[str], log: Optional[LogFn] = None) -> List[SourceItem]:
    """把目录/文件展开成待转换列表，并读取每个 ncm 的头部信息。"""
    found: Dict[str, SourceItem] = {}
    order: List[str] = []

    def add(full: str, rel_dir: str) -> None:
        key = os.path.normcase(os.path.abspath(full))
        if key in found:
            return
        found[key] = SourceItem(path=os.path.abspath(full), rel_dir=rel_dir)
        order.append(key)

    for raw in paths:
        path = os.path.abspath(raw)
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for filename in sorted(filenames):
                    if filename.lower().endswith(NCM_EXT):
                        rel = os.path.relpath(dirpath, path)
                        add(os.path.join(dirpath, filename), "" if rel == "." else rel)
        elif os.path.isfile(path) and path.lower().endswith(NCM_EXT):
            add(path, "")

    items = [found[key] for key in order]
    if log:
        log(f"扫描到 {len(items)} 个 ncm 文件")
    for item in items:
        try:
            song = parse_ncm(item.path)
            item.size = song.size
            item.audio_size = song.audio_size
            item.fmt = (song.audio_format or song.declared_format or "").lower()
            item.duration_ms = song.duration_ms
            item.bitrate = song.bitrate
            item.title = song.music_name
            item.artist = song.artist_text
            item.album = song.album
            item.cover_size = len(song.cover)
        except Exception as exc:  # 单个文件坏掉不影响其它文件
            item.error = str(exc)
            try:
                item.size = os.path.getsize(item.path)
            except OSError:
                item.size = 0
    return items


def artist_folder_name(item: SourceItem) -> str:
    """取歌手名作为子目录名（多歌手的取第一位，避免出现路径分隔符）。"""
    if not item.artist:
        return ""
    first = item.artist.replace(" / ", "|").split("|")[0].strip()
    return tagmod.sanitize_filename(first) if first else ""


def output_folder_for(item: SourceItem, options: ConvertOptions) -> str:
    """算出这个文件应该输出到哪个目录（两条路径推算逻辑共用，避免界面显示与实际不符）。"""
    if not options.output_dir:
        return os.path.dirname(item.path)
    folder = options.output_dir
    if item.rel_dir:
        if options.keep_structure:
            folder = os.path.join(folder, item.rel_dir)
    elif options.artist_subfolder:
        name = artist_folder_name(item)
        if name:
            folder = os.path.join(folder, name)
    return folder


def expected_output_path(item: SourceItem, options: ConvertOptions) -> str:
    """按当前设置推算「这个文件应该输出到哪里」。

    界面上用它提前标出哪些文件已经转换过，避免重复劳动。
    """
    ext = "." + (item.fmt or "mp3").lower()
    base = tagmod.sanitize_filename(item.song_display if options.use_song_name else item.stem)
    return os.path.join(output_folder_for(item, options), base + ext)


AUDIO_EXTENSIONS = (".mp3", ".flac", ".ogg", ".m4a", ".wav", ".ape", ".wv", ".aac", ".mp4")


def find_existing_audio(dst: str) -> Optional[str]:
    """目标位置是否已经有这首歌的转换结果；有就返回那个文件的路径。

    先看完全同名的文件；再看同目录下「文件名一样、只是音频后缀不同」的文件
    （比如之前用别的工具转成过 mp3，而这次源文件是无损 flac），避免重复转。
    """
    if os.path.exists(dst):
        return dst
    folder = os.path.dirname(dst) or "."
    stem = os.path.splitext(os.path.basename(dst))[0].lower()
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        lowered = name.lower()
        base, ext = os.path.splitext(lowered)
        if base == stem and ext in AUDIO_EXTENSIONS:
            return os.path.join(folder, name)
    return None


# --------------------------------------------------------------------------- #
# 转换器
# --------------------------------------------------------------------------- #

class Converter:
    """把一个或多个 SourceItem 转成 mp3/flac。"""

    def __init__(self, options: ConvertOptions):
        self.options = options
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._reserved: set = set()
        self.official_engine = None
        self.engine_note = ""
        if options.engine == "official":
            from .engines import EngineError, OfficialEngine
            try:
                self.official_engine = OfficialEngine()
                self.engine_note = f"官方引擎：{self.official_engine.dll_path}"
            except EngineError as exc:
                self.engine_note = f"官方引擎不可用，已回退到内置引擎（{exc}）"
        if self.official_engine is None:
            self.options.engine = "builtin"

    # -- 取消 -------------------------------------------------------------- #
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def canceled(self) -> bool:
        return self._cancel.is_set()

    # -- 输出路径 ---------------------------------------------------------- #
    def output_path_for(self, item: SourceItem, song: NCMSong, ext: str) -> str:
        stem = item.stem
        if self.options.use_song_name:
            base = tagmod.sanitize_filename(song.display_name() or stem)
        else:
            base = tagmod.sanitize_filename(stem)

        folder = output_folder_for(item, self.options)
        candidate = os.path.join(folder, base + ext)
        with self._lock:
            final = candidate
            index = 2
            while os.path.normcase(final) in self._reserved:
                final = os.path.join(folder, f"{base} ({index}){ext}")
                index += 1
            self._reserved.add(os.path.normcase(final))
        return final

    # -- 标签内容 ---------------------------------------------------------- #
    @staticmethod
    def tag_info(song: NCMSong, item: SourceItem) -> Dict[str, str]:
        title = song.music_name or item.stem
        artist = song.artist_text
        return {
            "title": title,
            "artist": artist,
            "album_artist": artist,
            "album": song.album,
            "track": str(song.meta.get("track") or ""),
            "year": str(song.meta.get("year") or song.meta.get("publishTime") or "")[:4],
            "encoder": ENCODER_TAG,
        }

    # -- 官方引擎 ---------------------------------------------------------- #
    def _render_official(self, item: SourceItem, dst: str) -> None:
        """调用官方 libncmdump.dll 产出成品，再改名成我们要的路径。

        官方库按元数据决定文件名，所以先导出到临时目录，拿到文件后原子替换到 dst。
        临时目录建在目标目录旁边（同一个盘），这样 os.replace 才是原子操作。
        """
        parent = os.path.dirname(dst) or "."
        os.makedirs(parent, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix=".ncm2mp3_tmp_", dir=parent)
        try:
            produced = self.official_engine.dump(item.path, workdir, self.options.write_tags)
            os.replace(produced, dst)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- 单个文件 ---------------------------------------------------------- #
    def convert_one(self, item: SourceItem, on_progress: Optional[ProgressFn] = None) -> FileResult:
        started = time.time()

        def fail(message: str) -> FileResult:
            return FileResult(src=item.path, status=FAILED, message=message,
                              seconds=time.time() - started, title=item.title, artist=item.artist)

        if item.error:
            return fail(f"解析失败：{item.error}")

        try:
            song = parse_ncm(item.path)
        except Exception as exc:
            return fail(f"解析失败：{exc}")

        fmt = (song.audio_format or song.declared_format or "mp3").lower()
        ext = "." + fmt
        dst = self.output_path_for(item, song, ext)

        if not self.options.overwrite:
            existing = find_existing_audio(dst)
            if existing:
                return FileResult(src=item.path, dst=existing, fmt=fmt, status=SKIPPED,
                                  message=f"已有转换结果（{os.path.basename(existing)}），已跳过",
                                  seconds=time.time() - started,
                                  out_size=os.path.getsize(existing),
                                  title=item.title, artist=item.artist)

        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        tmp = dst + ".part"
        try:
            if self.options.engine == "official" and self.official_engine is not None:
                self._render_official(item, dst)
            else:
                if os.path.exists(tmp):
                    os.remove(tmp)
                decrypt_audio_to_file(
                    song, tmp,
                    progress=(lambda done, total: on_progress(done / total)) if on_progress else None,
                    should_stop=self._cancel.is_set,
                )
                if self._cancel.is_set():
                    self._safe_remove(tmp)
                    return FileResult(src=item.path, dst=dst, fmt=fmt, status=CANCELED,
                                      message="已取消", seconds=time.time() - started,
                                      title=item.title, artist=item.artist)

                if self.options.write_tags:
                    info = self.tag_info(song, item)
                    if fmt == "mp3":
                        tagmod.write_id3v23(tmp, info, song.cover, song.cover_mime)
                    elif fmt == "flac":
                        tagmod.write_flac_tags(tmp, info, song.cover, song.cover_mime)

                os.replace(tmp, dst)

            message = ""
            if self.options.verify:
                message = self.verify_output(dst, fmt, song, item)

            if self.options.save_cover and song.cover:
                cover_path = os.path.splitext(dst)[0] + song.cover_ext
                with open(cover_path, "wb") as handle:
                    handle.write(song.cover)

            if self.options.delete_source:
                if message.startswith("⚠"):
                    # 自检没过就不动源文件，宁可留下 ncm 也不能只留一个残缺成品
                    message += "；输出未通过校验，已保留原 ncm"
                else:
                    self._safe_remove(item.path)

            return FileResult(src=item.path, dst=dst, fmt=fmt, status=OK, message=message,
                              seconds=time.time() - started, out_size=os.path.getsize(dst),
                              title=item.title, artist=item.artist)
        except Exception as exc:
            self._safe_remove(tmp)
            return fail(f"转换失败：{exc}")
        finally:
            if on_progress:
                on_progress(1.0)

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # -- 校验 -------------------------------------------------------------- #
    def verify_output(self, dst: str, fmt: str, song: NCMSong, item: SourceItem) -> str:
        """转换后自检。

        除了「格式头是否正常、标签是否写进去」，还要回答一个更重要的问题：
        **源文件本身是不是完整的**。网易云的下载如果中断，ncm 的头部和封面都在，
        只有音频正文被截断；这时元数据里的时长仍然写着完整值，照抄的 FLAC
        STREAMINFO 也写着完整值，光比对它们发现不了问题。所以这里真去数一遍
        帧、把实际覆盖的时长和元数据比一次。
        """
        notes: List[str] = []
        warnings: List[str] = []
        expect = song.duration_ms
        try:
            if fmt == "flac":
                info = flac_stream_info(dst)
                declared_ms = info["duration_ms"]
                if expect and declared_ms:
                    delta = abs(declared_ms - expect)
                    if delta > 2000:
                        warnings.append(f"时长与元数据相差 {delta / 1000:.1f}s")
                if not info["frame_sync_ok"]:
                    warnings.append("首个音频帧同步码异常")

                frames, measured = flac_frame_measure(dst, info["audio_offset"], info["channels"])
                measured_ms = int(measured * 1000 / info["sample_rate"]) if info["sample_rate"] else 0
                if frames < 8:
                    warnings.append(f"音频帧数异常（只找到 {frames} 帧）")
                elif info["samples"] and measured:
                    ratio = measured / info["samples"]
                    if ratio < 0.98:
                        warnings.append(
                            f"音频数据不完整：实际只覆盖约 {ratio * 100:.0f}%"
                            f"（{human_duration(measured_ms)} / {human_duration(declared_ms)}），"
                            "源文件很可能没有下载完"
                        )
                notes.append(
                    f"{info['sample_rate']}Hz/{info['channels']}声道/{info['bits']}bit "
                    f"{human_duration(measured_ms or declared_ms)}（{frames} 帧）"
                )
                if self.options.write_tags and tagmod.read_flac_tags(dst).get("TITLE") != (song.music_name or item.stem):
                    warnings.append("标签写入后读回不一致")
            elif fmt == "mp3":
                frames, samples, sample_rate, bitrate = mp3_measure(dst)
                if frames < 2:
                    warnings.append("MP3 帧头连续扫描失败")
                duration_ms = int(samples * 1000 / sample_rate) if sample_rate else 0
                notes.append(f"{frames} 帧 {sample_rate}Hz {bitrate}kbps {human_duration(duration_ms)}")
                if expect and duration_ms:
                    delta = abs(duration_ms - expect)
                    if delta > max(3000, expect * 0.02):
                        warnings.append(
                            f"实际时长 {human_duration(duration_ms)} 与元数据 "
                            f"{human_duration(expect)} 不符，源文件很可能没有下载完"
                        )
                if self.options.write_tags and tagmod.read_id3v23(dst).get("TIT2") != (song.music_name or item.stem):
                    warnings.append("标签写入后读回不一致")
        except Exception as exc:
            warnings.append(f"校验异常：{exc}")
        if warnings:
            return "⚠ " + "；".join(warnings)
        return "；".join(notes)

    # -- 批量 -------------------------------------------------------------- #
    def run(
        self,
        items: Sequence[SourceItem],
        on_start: Optional[Callable[[int, SourceItem], None]] = None,
        on_progress: Optional[Callable[[int, float], None]] = None,
        on_done: Optional[Callable[[int, FileResult], None]] = None,
        log: Optional[LogFn] = None,
    ) -> List[FileResult]:
        """并发转换，返回与 items 顺序一致的结果列表。"""
        results: List[Optional[FileResult]] = [None] * len(items)

        def work(pair) -> None:
            index, item = pair
            if self._cancel.is_set():
                results[index] = FileResult(src=item.path, status=CANCELED, message="已取消")
                if on_done:
                    on_done(index, results[index])
                return
            if on_start:
                on_start(index, item)
            if log:
                log(f"开始转换：{item.name}")
            result = self.convert_one(
                item,
                (lambda frac, idx=index: on_progress(idx, frac)) if on_progress else None,
            )
            results[index] = result
            if on_done:
                on_done(index, result)
            if log:
                if result.status == OK:
                    log(f"✔ {item.name} → {os.path.basename(result.dst)}"
                        + (f"  [{result.message}]" if result.message else ""))
                elif result.status == SKIPPED:
                    log(f"↷ 跳过：{item.name}（{result.message}）")
                elif result.status == CANCELED:
                    log(f"✖ 已取消：{item.name}")
                else:
                    log(f"✘ 失败：{item.name}（{result.message}）")

        jobs = max(1, min(self.options.jobs, max(1, len(items))))
        if jobs == 1:
            for pair in enumerate(items):
                work(pair)
        else:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(work, enumerate(items)))
        return [r for r in results if r is not None]


# --------------------------------------------------------------------------- #
# 校验辅助
# --------------------------------------------------------------------------- #

def flac_stream_info(path: str) -> Dict[str, int]:
    """读取 FLAC 的 STREAMINFO，并检查音频区首个帧同步码。

    FLAC 的元数据区可能有多个块（STREAMINFO/SEEKTABLE/PADDING...），
    必须一路走到最后一块，紧跟其后的才是音频帧。
    """
    with open(path, "rb") as handle:
        if handle.read(4) != b"fLaC":
            raise ValueError("缺少 fLaC 标记")
        info = None
        while True:
            header = handle.read(4)
            if len(header) < 4:
                raise ValueError("FLAC 元数据块不完整")
            is_last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            length = int.from_bytes(header[1:4], "big")
            if block_type == 0 and info is None:
                data = handle.read(length)
                if len(data) < 18:
                    raise ValueError("STREAMINFO 数据不完整")
                packed = int.from_bytes(data[10:18], "big")
                sample_rate = packed >> 44
                info = {
                    "sample_rate": sample_rate,
                    "channels": ((packed >> 41) & 0x07) + 1,
                    "bits": ((packed >> 36) & 0x1F) + 1,
                    "samples": packed & ((1 << 36) - 1),
                    "duration_ms": int((packed & ((1 << 36) - 1)) * 1000 / sample_rate) if sample_rate else 0,
                }
            else:
                handle.seek(length, os.SEEK_CUR)
            if is_last:
                break
        audio_offset = handle.tell()
        sync = handle.read(2)
    if info is None:
        raise ValueError("缺少 STREAMINFO 块")
    info["audio_offset"] = audio_offset
    info["frame_sync_ok"] = len(sync) == 2 and sync[0] == 0xFF and (sync[1] & 0xFE) == 0xF8
    return info


# FLAC 帧头里的块大小代码 → 采样数
_FLAC_BLOCKSIZE_CODE = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608,
                        8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192,
                        14: 16384, 15: 32768}


def _crc8(data: bytes) -> int:
    """FLAC 帧头校验用的 CRC-8（多项式 0x07）。"""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_frame_header(data: bytes, pos: int, channels: int = 0):
    """解析 pos 处的 FLAC 帧头，返回 (该帧采样数, 帧头长度) 或 None。"""
    if pos + 5 > len(data):
        return None
    if data[pos] != 0xFF or (data[pos + 1] & 0xFE) != 0xF8 or (data[pos + 1] & 0x02):
        return None
    blocksize_code = data[pos + 2] >> 4
    sample_rate_code = data[pos + 2] & 0x0F
    channel_code = data[pos + 3] >> 4
    sample_size_code = (data[pos + 3] >> 1) & 0x07
    if data[pos + 3] & 0x01:
        return None
    if blocksize_code == 0 or sample_rate_code == 15 or sample_size_code == 3:
        return None
    if channel_code > 10:
        return None
    # 声道分配必须和 STREAMINFO 对得上，用来排除把帧体里的数据误判成帧头
    if channels:
        if channel_code < 8:
            if channel_code + 1 != channels:
                return None
        elif channels != 2:
            return None

    index = pos + 4
    first = data[index]
    if first < 0x80:
        index += 1
    else:
        mask, count = 0x80, 0
        while first & mask:
            count += 1
            mask >>= 1
        if count < 2 or count > 7:
            return None
        index += count
    if index + 2 > len(data):
        return None

    blocksize = None
    if blocksize_code == 6:
        blocksize = data[index] + 1
        index += 1
    elif blocksize_code == 7:
        blocksize = int.from_bytes(data[index:index + 2], "big") + 1
        index += 2
    if sample_rate_code == 12:
        index += 1
    elif sample_rate_code in (13, 14):
        index += 2
    if index >= len(data):
        return None
    if _crc8(data[pos:index]) != data[index]:
        return None
    if blocksize is None:
        blocksize = _FLAC_BLOCKSIZE_CODE.get(blocksize_code)
        if blocksize is None:
            return None
    return blocksize, index + 1 - pos


def flac_frame_measure(path: str, audio_offset: int, channels: int = 0):
    """统计 FLAC 音频区实际有多少帧、覆盖多少采样。

    STREAMINFO 里写的是「完整文件应有」的总采样数，而它是从源文件原样复制过来的，
    所以文件被截断时它依然显示完整时长。只有真去数一遍帧，才能发现源文件没下完。
    """
    frames = 0
    samples = 0
    block_size = 8 << 20
    overlap = 32
    with open(path, "rb") as handle:
        handle.seek(audio_offset)
        tail = b""
        while True:
            chunk = handle.read(block_size)
            if not chunk:
                break
            data = tail + chunk
            pos = 0
            while True:
                index = data.find(b"\xff", pos)
                if index < 0 or index + 16 > len(data):
                    break
                parsed = _flac_frame_header(data, index, channels)
                if parsed is None:
                    pos = index + 1
                    continue
                frame_samples, header_len = parsed
                frames += 1
                samples += frame_samples
                pos = index + header_len
            tail = data[-overlap:]
    return frames, samples


_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}

# 注意：帧头里的 layer 字段是 3=Layer I、2=Layer II、1=Layer III
_BR_L1_MPEG1 = (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448)
_BR_L2_MPEG1 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)
_BR_L3_MPEG1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_BR_L1_MPEG2 = (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256)
_BR_L23_MPEG2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)

_BITRATES = {
    (3, 1): _BR_L3_MPEG1, (3, 2): _BR_L2_MPEG1, (3, 3): _BR_L1_MPEG1,
    (2, 1): _BR_L23_MPEG2, (2, 2): _BR_L23_MPEG2, (2, 3): _BR_L1_MPEG2,
    (0, 1): _BR_L23_MPEG2, (0, 2): _BR_L23_MPEG2, (0, 3): _BR_L1_MPEG2,
}


def _mp3_frame(data: bytes, pos: int):
    """解析 pos 处的 MP3 帧头，返回 (帧长, 采样数, 码率, 采样率) 或 None。"""
    if pos + 4 > len(data):
        return None
    if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
        return None
    header = int.from_bytes(data[pos:pos + 4], "big")
    version = (header >> 19) & 0x03
    layer = (header >> 17) & 0x03
    bitrate_index = (header >> 12) & 0x0F
    rate_index = (header >> 10) & 0x03
    padding = (header >> 9) & 0x01
    if version == 1 or layer == 0 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate_row = _BITRATES.get((version, layer))
    rates = _SAMPLE_RATES.get(version)
    if not bitrate_row or not rates:
        return None
    bitrate = bitrate_row[bitrate_index]
    sample_rate = rates[rate_index]
    if not bitrate or not sample_rate:
        return None
    if layer == 3:            # Layer I
        length = (12 * bitrate * 1000 // sample_rate + padding) * 4
        samples = 384
    elif layer == 2:          # Layer II
        length = 144 * bitrate * 1000 // sample_rate + padding
        samples = 1152
    else:                     # Layer III
        samples = 1152 if version == 3 else 576
        length = (samples // 8) * bitrate * 1000 // sample_rate + padding
    if length <= 4:
        return None
    return length, samples, bitrate, sample_rate


def mp3_measure(path: str, max_frames: int = 4_000_000):
    """从第一个同步字开始把整条帧链走完，返回 (帧数, 总采样数, 采样率, 码率)。

    必须走完整条链：只数前几百帧得到的是「前几秒」，既会低估整首时长，
    也发现不了源文件被截断。
    """
    with open(path, "rb") as handle:
        head = handle.read(10)
        handle.seek(tagmod.id3_tag_size(head))
        buffer = b""
        pos = 0
        eof = False
        frames = 0
        samples = 0
        sample_rate = 0
        bitrate = 0
        while frames < max_frames:
            if pos >= (1 << 19):          # 丢掉已处理的数据，控制内存占用
                buffer = buffer[pos:]
                pos = 0
            if pos + 4 > len(buffer):
                if eof:
                    break
                chunk = handle.read(1 << 20)
                if not chunk:
                    eof = True
                    continue
                buffer += chunk
                continue
            parsed = _mp3_frame(buffer, pos)
            if parsed is None:
                if frames:
                    break                 # 帧链断了（正常的尾部标签或文件不完整）
                pos += 1
                continue
            length, frame_samples, frame_bitrate, frame_rate = parsed
            if pos + length > len(buffer):
                if eof:
                    break
                buffer = buffer[pos:]
                pos = 0
                chunk = handle.read(1 << 20)
                if not chunk:
                    eof = True
                buffer += chunk
                continue
            frames += 1
            samples += frame_samples
            sample_rate = frame_rate
            bitrate = frame_bitrate
            pos += length
    return frames, samples, sample_rate, bitrate
