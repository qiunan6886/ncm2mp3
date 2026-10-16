"""完整性检测测试：验证「源文件被截断」能被发现，完整文件不会误报。

用法：
    python tools/integrity_test.py <源目录>
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm2mp3.core import (  # noqa: E402
    ConvertOptions, Converter, flac_frame_measure, flac_stream_info, human_duration,
    mp3_measure, parse_ncm, scan,
)


def truncate(source: str, target: str, ratio: float) -> int:
    """按比例截断音频正文，保留完整的头部与封面。"""
    song = parse_ncm(source)
    keep = song.audio_offset + int(song.audio_size * ratio)
    with open(source, "rb") as src, open(target, "wb") as dst:
        remaining = keep
        while remaining > 0:
            chunk = src.read(min(1 << 20, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)
    return keep


def describe(path: str, fmt: str, song) -> str:
    if fmt == "flac":
        info = flac_stream_info(path)
        frames, measured = flac_frame_measure(path, info["audio_offset"], info["channels"])
        declared = info["samples"]
        measured_ms = int(measured * 1000 / info["sample_rate"]) if info["sample_rate"] else 0
        return (f"声明 {human_duration(info['duration_ms'])} / 实测 {human_duration(measured_ms)}"
                f"（{frames} 帧，覆盖 {measured / declared * 100 if declared else 0:.1f}%）"
                f"｜元数据 {human_duration(song.duration_ms)}")
    frames, samples, rate, bitrate = mp3_measure(path)
    measured_ms = int(samples * 1000 / rate) if rate else 0
    return (f"实测 {human_duration(measured_ms)}（{frames} 帧 {bitrate}kbps）"
            f"｜元数据 {human_duration(song.duration_ms)}")


def main() -> int:
    source_root = sys.argv[1] if len(sys.argv) > 1 else "."
    items = [i for i in scan([source_root]) if not i.error]
    picks = []
    for fmt in ("mp3", "flac"):
        for item in items:
            if item.fmt == fmt and item.audio_size < 140 * 1024 * 1024:
                picks.append(item)
                break
    if len(picks) < 2:
        print("需要同时找到 mp3 与 flac 样本")
        return 2

    workdir = tempfile.mkdtemp(prefix="ncm2mp3_integrity_")
    failures = 0
    try:
        good_dir = os.path.join(workdir, "good")
        bad_dir = os.path.join(workdir, "bad")
        print("=" * 78)
        print("① 完整文件：应当没有任何 ⚠ 警告")
        print("=" * 78)
        options = ConvertOptions(output_dir=good_dir, overwrite=True, verify=True, jobs=2)
        results = Converter(options).run(picks)
        for result in results:
            song = parse_ncm(result.src)
            ok = result.status == "ok" and not result.message.startswith("⚠")
            failures += 0 if ok else 1
            print(f"{'✔' if ok else '✘'} {os.path.basename(result.src)}")
            print(f"    自检：{result.message}")
            print(f"    {describe(result.dst, result.fmt, song)}")

        print()
        print("=" * 78)
        print("② 截断到 40% 音频：必须报出「不完整」警告")
        print("=" * 78)
        broken_dir = os.path.join(workdir, "broken_src")
        os.makedirs(broken_dir, exist_ok=True)
        broken = []
        for item in picks:
            target = os.path.join(broken_dir, "截断_" + item.name)
            kept = truncate(item.path, target, 0.4)
            broken.append(target)
            print(f"  造出截断样本：{os.path.basename(target)}（保留 {kept / 1024 / 1024:.1f} MB）")

        options = ConvertOptions(output_dir=bad_dir, overwrite=True, verify=True, jobs=2,
                                 delete_source=True)
        results = Converter(options).run(scan(broken))
        for result in results:
            song = parse_ncm(result.src)
            caught = result.message.startswith("⚠")
            survived = os.path.exists(result.src)
            ok = caught and survived
            failures += 0 if ok else 1
            print(f"{'✔' if ok else '✘'} {os.path.basename(result.src)}")
            print(f"    自检：{result.message}")
            print(f"    {describe(result.dst, result.fmt, song)}")
            print(f"    源文件是否保留：{'是' if survived else '否（不该被删！）'}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("结论：", "✔ 全部符合预期" if failures == 0 else f"✘ 有 {failures} 项不符合预期")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
