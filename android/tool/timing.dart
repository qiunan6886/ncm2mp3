// 分步计时：定位 Dart 核心各环节耗时。
//   dart run tool/timing.dart <ncm 文件>

import 'dart:io';

import 'package:ncm2mp3/ncm_convert.dart';
import 'package:ncm2mp3/ncm_core.dart';
import 'package:ncm2mp3/ncm_tags.dart';

void millis(String label, void Function() body) {
  final sw = Stopwatch()..start();
  body();
  sw.stop();
  stdout.writeln('  $label: ${sw.elapsedMilliseconds} ms');
}

void main(List<String> args) {
  final path = args.first;
  stdout.writeln('文件：$path');
  NcmSong? song;
  millis('解析头部', () => song = parseNcm(path));
  final s = song!;
  stdout.writeln('  格式=${s.audioFormat} 时长=${s.durationMs}ms 音频=${s.audioSize} 字节 封面=${s.cover.length}');

  final tmp = '${Directory.systemTemp.path}${Platform.pathSeparator}ncm_timing.${s.audioFormat}';
  millis('解密音频', () => decryptAudioToFile(s, tmp));
  millis('写标签', () {
    writeId3v23IfNeeded(tmp, s);
  });
  final size = File(tmp).lengthSync();
  stdout.writeln('  产物 $size 字节');

  if (s.audioFormat == 'mp3') {
    millis('MP3 走帧', () {
      final m = mp3Measure(tmp);
      stdout.writeln('    帧=${m[0]} 采样=${m[1]} 采样率=${m[2]} 码率=${m[3]}');
    });
  } else {
    millis('FLAC 数帧', () {
      final info = flacStreamInfo(tmp);
      final measured = flacFrameMeasure(tmp, info.audioOffset, info.channels);
      stdout.writeln('    帧=${measured[0]} 采样=${measured[1]} 声明=${info.samples}');
    });
  }
  File(tmp).deleteSync();
}

void writeId3v23IfNeeded(String path, NcmSong song) {
  final info = <String, String>{
    'title': song.musicName,
    'artist': song.artistText,
    'album_artist': song.artistText,
    'album': song.album,
    'encoder': kEncoderTag,
  };
  if (song.audioFormat == 'mp3') {
    writeId3v23(path, info, song.cover, song.coverMime);
  } else if (song.audioFormat == 'flac') {
    writeFlacTags(path, info, song.cover, song.coverMime);
  }
}
