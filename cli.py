"""命令行入口：不用图形界面也能跑，方便批处理和自检。

示例：
    python cli.py scan "G:\\Music\\网易云"
    python cli.py convert "G:\\Music\\网易云" -o "D:\\Music-out" --jobs 4
    python cli.py convert a.ncm b.ncm --overwrite --delete-source
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ncm2mp3 import dedup  # noqa: E402
from ncm2mp3.core import (  # noqa: E402
    CANCELED, FAILED, OK, SKIPPED, ConvertOptions, Converter, describe_format,
    human_duration, human_size, scan,
)


def _enable_utf8_console() -> None:
    """让命令行输出在中文 Windows 的 GBK 控制台下也不会崩。

    默认控制台用 GBK，打印 ✔ / ✘ 这类字符会抛 UnicodeEncodeError。
    这里先把控制台切到 UTF-8；切不过去就退而求其次，把编码错误改成替换输出。
    """
    switched = False
    if os.name == "nt":
        try:
            import ctypes

            switched = bool(ctypes.windll.kernel32.SetConsoleOutputCP(65001))
        except Exception:
            switched = False
    for stream in (sys.stdout, sys.stderr):
        try:
            if switched:
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def cmd_scan(args) -> int:
    items = scan(args.paths, log=print)
    print()
    print(f"{'格式':<7}{'码率':>8}{'时长':>9}{'大小':>12}  歌曲")
    print("-" * 96)
    for item in items:
        if item.error:
            print(f"{'!!':<7}{'-':>8}{'-':>9}{human_size(item.size):>12}  {item.name}  <- {item.error}")
            continue
        bitrate = f"{item.bitrate // 1000}k" if item.bitrate else "-"
        print(f"{describe_format(item.fmt):<7}{bitrate:>8}{item.duration_text:>9}"
              f"{human_size(item.size):>12}  {item.song_display}")
    print("-" * 96)
    total = sum(1 for i in items if not i.error)
    print(f"共 {len(items)} 个文件，可用 {total} 个")
    return 0


def cmd_convert(args) -> int:
    started = time.time()
    items = scan(args.paths, log=print)
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有可转换的文件")
        return 1

    broken = [item for item in items if item.error]
    if broken:
        print(f"注意：以下 {len(broken)} 个文件无法解析，将按失败处理（不计入成功）：")
        for item in broken[:10]:
            print(f"  - {item.name}：{item.error}")
        if len(broken) > 10:
            print(f"  …… 其余 {len(broken) - 10} 个省略")
        print()

    options = ConvertOptions(
        output_dir=os.path.abspath(args.output) if args.output else None,
        keep_structure=not args.flat,
        use_song_name=args.rename,
        overwrite=args.overwrite,
        delete_source=args.delete_source,
        write_tags=not args.no_tags,
        save_cover=args.cover,
        verify=not args.no_verify,
        jobs=args.jobs,
        engine=args.engine,
        artist_subfolder=args.artist_folder,
    )
    print(f"输出目录：{options.output_dir or '（与源文件同一目录）'}")
    print(f"并发线程：{options.jobs}   标签：{'写' if options.write_tags else '不写'}"
          f"   校验：{'开' if options.verify else '关'}")
    print()

    converter = Converter(options)
    done_count = 0

    def on_done(index, result):
        nonlocal done_count
        done_count += 1
        mark = {OK: "✔", SKIPPED: "↷", CANCELED: "✖", FAILED: "✘"}.get(result.status, "?")
        target = os.path.basename(result.dst) if result.dst else "-"
        extra = f"  [{result.message}]" if result.message else ""
        print(f"[{done_count:{len(str(len(items)))}}/{len(items)}] {mark} {os.path.basename(result.src)}"
              f" → {target}   {result.seconds:.1f}s{extra}")

    try:
        results = converter.run(items, on_done=on_done, log=None)
    except KeyboardInterrupt:
        converter.cancel()
        print("\n已中断")
        return 130

    ok = sum(1 for r in results if r.status == OK)
    skipped = sum(1 for r in results if r.status == SKIPPED)
    failed = sum(1 for r in results if r.status == FAILED)
    print()
    print(f"完成：成功 {ok} / 跳过 {skipped} / 失败 {failed}，用时 {time.time() - started:.1f}s")
    if failed:
        for r in results:
            if r.status == FAILED:
                print(f"  失败：{r.src}（{r.message}）")
    return 0 if failed == 0 else 1


def cmd_dedup(args) -> int:
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print(f"目录不存在：{root}")
        return 1
    print(f"扫描重复：{root}")
    last = [0]

    def progress(done, total):
        if done != last[0]:
            last[0] = done

    groups = dedup.scan_duplicates(root, on_progress=progress)
    if not groups:
        print("没有发现重复文件")
        return 0

    print()
    print(f"{'类别':<20}{'保留':<38}{'可删除':<30}{'可释放':>10}")
    print("-" * 104)
    for group in groups:
        mark = "✔" if group.auto else " "
        print(f"{mark}{group.category:<19}{group.keep.name[:36]:<38}"
              f"{group.describe_remove(1)[:28]:<30}{human_size(group.free_bytes):>10}")
    print("-" * 104)

    auto_groups = [g for g in groups if g.auto]
    chosen = groups if args.all else auto_groups
    bytes_total = sum(g.free_bytes for g in chosen)
    count = sum(len(g.remove) for g in chosen)
    print(f"共 {len(groups)} 组；其中「内容相同」{len(auto_groups)} 组（默认选中）")
    print(f"本次将处理 {len(chosen)} 组、{count} 个文件，可释放 {human_size(bytes_total)}")
    if not args.all:
        print("（加 --all 可连「内容不同 / 不同格式 / 跨目录」的那些一起处理）")

    if not args.apply:
        print()
        print("当前是预演模式，没有删除任何文件。确认无误后加 --apply 执行。")
        print("执行时文件会被移入 Windows 回收站，可随时恢复。")
        return 0

    targets = [f.path for group in chosen for f in group.remove]
    if not targets:
        print("没有需要删除的文件")
        return 0
    moved, failed = dedup.move_to_recycle_bin(targets)
    print()
    print(f"已移入回收站：{moved} 个")
    if failed:
        print(f"失败 {len(failed)} 个：")
        for path in failed[:10]:
            print(f"  - {path}")
        return 1
    return 0


def main() -> int:
    _enable_utf8_console()
    parser = argparse.ArgumentParser(prog="ncm2mp3", description="网易云 NCM 转 MP3/FLAC（保持原始格式与音质）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描并列出文件")
    p_scan.add_argument("paths", nargs="+")
    p_scan.set_defaults(func=cmd_scan)

    p_conv = sub.add_parser("convert", help="执行转换")
    p_conv.add_argument("paths", nargs="+")
    p_conv.add_argument("-o", "--output", help="输出目录（默认与源文件同目录）")
    p_conv.add_argument("--flat", action="store_true", help="输出目录下不保留子目录结构")
    p_conv.add_argument("--artist-folder", action="store_true",
                        help="源目录直属的文件，按歌手归入子目录")
    p_conv.add_argument("--rename", action="store_true", help="用「歌手 - 歌名」命名")
    p_conv.add_argument("--overwrite", action="store_true", help="覆盖同名文件")
    p_conv.add_argument("--delete-source", action="store_true", help="成功后删除原 ncm")
    p_conv.add_argument("--no-tags", action="store_true", help="不写标签与封面")
    p_conv.add_argument("--cover", action="store_true", help="额外导出封面图片")
    p_conv.add_argument("--no-verify", action="store_true", help="跳过转换后校验")
    p_conv.add_argument("--jobs", type=int, default=4, help="并发线程数（默认 4）")
    p_conv.add_argument("--limit", type=int, help="只处理前 N 个（试跑用）")
    p_conv.add_argument("--engine", choices=("builtin", "official"), default="builtin",
                        help="转换引擎：builtin=内置（默认），official=官方 libncmdump.dll")
    p_conv.set_defaults(func=cmd_convert)

    p_dedup = sub.add_parser("dedup", help="扫描重复音乐（默认只预演，不删除）")
    p_dedup.add_argument("path", help="要扫描的目录")
    p_dedup.add_argument("--apply", action="store_true",
                         help="真正执行：把选中的重复文件移入回收站")
    p_dedup.add_argument("--all", action="store_true",
                         help="连「内容不同 / 不同格式 / 跨目录同名」的也一起处理")
    p_dedup.set_defaults(func=cmd_dedup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
