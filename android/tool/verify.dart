// 桌面验证工具：用 Dart 核心把 ncm 转出来，便于和官方 ncmdump 逐字节比对。
//
// 用法（在 android 目录下）：
//   dart run tool/verify.dart <输出目录> <ncm 文件> [更多 ncm...]
//
// 它不依赖 Flutter，只用到 dart:io，所以可以直接在桌面 Dart VM 上跑。

import 'dart:io';

import 'package:ncm2mp3/ncm_convert.dart';

void main(List<String> args) {
  if (args.length < 2) {
    stdout.writeln('用法: dart run tool/verify.dart <输出目录> <ncm 文件> [...]');
    exit(2);
  }
  final outputDir = Directory(args.first).absolute.path;
  final files = args.sublist(1);
  final options = ConvertOptions(outputDir: outputDir, keepStructure: false, verify: true);
  final converter = Converter(options);

  var failed = 0;
  final started = DateTime.now();
  for (final path in files) {
    final items = scan([path]);
    if (items.isEmpty) {
      stdout.writeln('✘ 找不到：$path');
      failed++;
      continue;
    }
    final item = items.first;
    if (item.error.isNotEmpty) {
      stdout.writeln('✘ 解析失败：${item.name}（${item.error}）');
      failed++;
      continue;
    }
    final result = converter.convertOne(item, onProgress: (_) {});
    final mark = result.status == 'ok' ? '✔' : (result.status == 'skipped' ? '↷' : '✘');
    stdout.writeln('$mark ${item.name} → ${result.dst}  [${result.status}] ${result.message}');
    if (result.status != 'ok' && result.status != 'skipped') failed++;
  }
  final seconds = DateTime.now().difference(started).inMilliseconds / 1000.0;
  stdout.writeln('完成：${files.length - failed}/${files.length}，用时 ${seconds.toStringAsFixed(1)}s');
  exit(failed == 0 ? 0 : 1);
}
