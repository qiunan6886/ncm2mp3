// 读回产物里的标签，检查写入是否正确。
//   dart run tool/tagcheck.dart <产物文件...>

import 'dart:io';

import 'package:ncm2mp3/ncm_tags.dart';

void main(List<String> args) {
  for (final path in args) {
    stdout.writeln('=== ${path.split(RegExp(r'[\\/]')).last} ===');
    if (path.toLowerCase().endsWith('.mp3')) {
      final tags = readId3v23(path);
      for (final entry in tags.entries) {
        final value = entry.value;
        final codes = value.runes.take(6).map((r) => r.toRadixString(16)).join(' ');
        stdout.writeln('  ${entry.key} = "$value"   码点: $codes');
      }
    } else {
      final tags = readFlacTags(path);
      for (final entry in tags.entries) {
        stdout.writeln('  ${entry.key} = "${entry.value}"');
      }
    }
  }
}
