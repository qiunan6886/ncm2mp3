// 转换调度：扫描、跳过已转换、批量转换、转换后自检。
// 与 Windows 版 core.py 的行为保持一致（不重编码、保持原格式与音质）。

import 'dart:io';
import 'dart:typed_data';

import 'ncm_core.dart';
import 'ncm_tags.dart';

const String kEncoderTag = 'ncm2mp3';
const List<String> kAudioExtensions = [
  '.mp3', '.flac', '.ogg', '.m4a', '.wav', '.ape', '.wv', '.aac', '.mp4',
];

String humanSize(int bytes) {
  if (bytes <= 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var value = bytes.toDouble();
  var index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return index == 0 ? '${value.toInt()} B' : '${value.toStringAsFixed(1)} ${units[index]}';
}

String humanDuration(int ms) {
  if (ms <= 0) return '-';
  final total = ms ~/ 1000;
  final seconds = (total % 60).toString().padLeft(2, '0');
  return '${total ~/ 60}:$seconds';
}

String describeFormat(String fmt) {
  switch (fmt.toLowerCase()) {
    case 'mp3':
      return 'MP3';
    case 'flac':
      return 'FLAC';
    case 'ogg':
      return 'OGG';
    case 'm4a':
      return 'M4A';
    case 'wav':
      return 'WAV';
    default:
      return fmt.isEmpty ? '未知' : fmt.toUpperCase();
  }
}

String _stemOf(String path) {
  final name = path.split(RegExp(r'[\\/]')).last;
  final dot = name.lastIndexOf('.');
  return dot > 0 ? name.substring(0, dot) : name;
}

// --------------------------------------------------------------------------- //
// 数据模型
// --------------------------------------------------------------------------- //

class SourceItem {
  String path;
  String relDir;
  int size;
  int audioSize;
  String fmt;
  int durationMs;
  int bitrate;
  String title;
  String artist;
  String album;
  int coverSize;
  String error;

  SourceItem({
    required this.path,
    this.relDir = '',
    this.size = 0,
    this.audioSize = 0,
    this.fmt = '',
    this.durationMs = 0,
    this.bitrate = 0,
    this.title = '',
    this.artist = '',
    this.album = '',
    this.coverSize = 0,
    this.error = '',
  });

  String get name => path.split(RegExp(r'[\\/]')).last;
  String get stem => _stemOf(path);

  String get songDisplay {
    if (title.isNotEmpty) return artist.isEmpty ? title : '$artist - $title';
    return stem;
  }

  String get fmtText => error.isNotEmpty ? '无法解析' : describeFormat(fmt);
  String get sizeText => humanSize(size);
  String get durationText => humanDuration(durationMs);

  Map<String, dynamic> toJson() => {
        'path': path, 'relDir': relDir, 'size': size, 'audioSize': audioSize,
        'fmt': fmt, 'durationMs': durationMs, 'bitrate': bitrate, 'title': title,
        'artist': artist, 'album': album, 'coverSize': coverSize, 'error': error,
      };

  static SourceItem fromJson(Map<String, dynamic> json) => SourceItem(
        path: json['path'] as String,
        relDir: (json['relDir'] ?? '') as String,
        size: (json['size'] ?? 0) as int,
        audioSize: (json['audioSize'] ?? 0) as int,
        fmt: (json['fmt'] ?? '') as String,
        durationMs: (json['durationMs'] ?? 0) as int,
        bitrate: (json['bitrate'] ?? 0) as int,
        title: (json['title'] ?? '') as String,
        artist: (json['artist'] ?? '') as String,
        album: (json['album'] ?? '') as String,
        coverSize: (json['coverSize'] ?? 0) as int,
        error: (json['error'] ?? '') as String,
      );
}

class ConvertOptions {
  String? outputDir;
  bool keepStructure;
  bool useSongName;
  bool overwrite;
  bool deleteSource;
  bool writeTags;
  bool verify;
  bool artistSubfolder;

  ConvertOptions({
    this.outputDir,
    this.keepStructure = true,
    this.useSongName = false,
    this.overwrite = false,
    this.deleteSource = false,
    this.writeTags = true,
    this.verify = true,
    this.artistSubfolder = false,
  });

  Map<String, dynamic> toJson() => {
        'outputDir': outputDir,
        'keepStructure': keepStructure,
        'useSongName': useSongName,
        'overwrite': overwrite,
        'deleteSource': deleteSource,
        'writeTags': writeTags,
        'verify': verify,
        'artistSubfolder': artistSubfolder,
      };

  static ConvertOptions fromJson(Map<String, dynamic> json) => ConvertOptions(
        outputDir: json['outputDir'] as String?,
        keepStructure: (json['keepStructure'] ?? true) as bool,
        useSongName: (json['useSongName'] ?? false) as bool,
        overwrite: (json['overwrite'] ?? false) as bool,
        deleteSource: (json['deleteSource'] ?? false) as bool,
        writeTags: (json['writeTags'] ?? true) as bool,
        verify: (json['verify'] ?? true) as bool,
        artistSubfolder: (json['artistSubfolder'] ?? false) as bool,
      );
}

class FileResult {
  final String src;
  final String dst;
  final String fmt;
  final String status; // ok / skipped / failed / canceled
  final String message;
  final double seconds;
  final int outSize;

  FileResult(this.src, this.dst, this.fmt, this.status, this.message, this.seconds, this.outSize);
}

// --------------------------------------------------------------------------- //
// 扫描
// --------------------------------------------------------------------------- //

List<SourceItem> scan(List<String> roots, {void Function(String)? log}) {
  final found = <String, SourceItem>{};
  final order = <String>[];

  void add(String full, String relDir) {
    final key = full.toLowerCase();
    if (found.containsKey(key)) return;
    found[key] = SourceItem(path: full, relDir: relDir);
    order.add(key);
  }

  for (final raw in roots) {
    final dir = Directory(raw);
    if (dir.existsSync()) {
      final rootPath = dir.absolute.path;
      for (final entity in dir.listSync(recursive: true, followLinks: false)) {
        if (entity is! File) continue;
        if (!entity.path.toLowerCase().endsWith('.ncm')) continue;
        final parent = entity.parent.absolute.path;
        var rel = parent == rootPath ? '' : parent.substring(rootPath.length).replaceAll(RegExp(r'^[\\/]+'), '');
        add(entity.absolute.path, rel);
      }
    } else if (File(raw).existsSync() && raw.toLowerCase().endsWith('.ncm')) {
      add(File(raw).absolute.path, '');
    }
  }

  final items = [for (final key in order) found[key]!];
  log?.call('扫描到 ${items.length} 个 ncm 文件');
  for (final item in items) {
    try {
      final song = parseNcm(item.path);
      item.size = song.size;
      item.audioSize = song.audioSize;
      item.fmt = song.audioFormat.toLowerCase();
      item.durationMs = song.durationMs;
      item.bitrate = song.bitrate;
      item.title = song.musicName;
      item.artist = song.artistText;
      item.album = song.album;
      item.coverSize = song.cover.length;
    } catch (exc) {
      item.error = exc.toString();
      try {
        item.size = File(item.path).lengthSync();
      } catch (_) {
        item.size = 0;
      }
    }
  }
  return items;
}

// --------------------------------------------------------------------------- //
// 输出路径 / 跳过已转换
// --------------------------------------------------------------------------- //

String artistFolderName(SourceItem item) {
  if (item.artist.isEmpty) return '';
  final first = item.artist.replaceAll(' / ', '|').split('|').first.trim();
  return first.isEmpty ? '' : sanitizeFileName(first);
}

String outputFolderFor(SourceItem item, ConvertOptions options) {
  final outputDir = options.outputDir;
  if (outputDir == null || outputDir.isEmpty) {
    return File(item.path).parent.path;
  }
  var folder = outputDir;
  if (item.relDir.isNotEmpty) {
    if (options.keepStructure) folder = '$folder${Platform.pathSeparator}${item.relDir}';
  } else if (options.artistSubfolder) {
    final name = artistFolderName(item);
    if (name.isNotEmpty) folder = '$folder${Platform.pathSeparator}$name';
  }
  return folder;
}

String expectedOutputPath(SourceItem item, ConvertOptions options) {
  final ext = '.${item.fmt.isEmpty ? 'mp3' : item.fmt}';
  final base = sanitizeFileName(options.useSongName ? item.songDisplay : item.stem);
  return '${outputFolderFor(item, options)}${Platform.pathSeparator}$base$ext';
}

/// 目标位置是否已有这首歌的成品（同后缀或换后缀都算）。
String? findExistingAudio(String dst) {
  if (File(dst).existsSync()) return dst;
  final file = File(dst);
  final folder = file.parent;
  if (!folder.existsSync()) return null;
  final stem = _stemOf(dst).toLowerCase();
  try {
    for (final entity in folder.listSync()) {
      if (entity is! File) continue;
      final name = entity.path.split(RegExp(r'[\\/]')).last;
      final dot = name.lastIndexOf('.');
      if (dot <= 0) continue;
      final base = name.substring(0, dot).toLowerCase();
      final ext = name.substring(dot).toLowerCase();
      if (base == stem && kAudioExtensions.contains(ext)) return entity.path;
    }
  } catch (_) {
    return null;
  }
  return null;
}

// --------------------------------------------------------------------------- //
// 校验：MP3 走帧 / FLAC 数帧
// --------------------------------------------------------------------------- //

const Map<int, List<int>> _sampleRates = {
  3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000],
};

const List<int> _brL1Mpeg1 = [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448];
const List<int> _brL2Mpeg1 = [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384];
const List<int> _brL3Mpeg1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320];
const List<int> _brL1Mpeg2 = [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256];
const List<int> _brL23Mpeg2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160];

// 注意：Dart 的 Map 用 List 作键是按引用比较的，这里必须用字符串键
const Map<String, List<int>> _bitrates = {
  '3,1': _brL3Mpeg1, '3,2': _brL2Mpeg1, '3,3': _brL1Mpeg1,
  '2,1': _brL23Mpeg2, '2,2': _brL23Mpeg2, '2,3': _brL1Mpeg2,
  '0,1': _brL23Mpeg2, '0,2': _brL23Mpeg2, '0,3': _brL1Mpeg2,
};

/// 返回 (帧长, 采样数, 码率, 采样率)，不是有效帧头时返回 null。
List<int>? _mp3Frame(Uint8List data, int pos) {
  if (pos + 4 > data.length) return null;
  if (data[pos] != 0xff || (data[pos + 1] & 0xe0) != 0xe0) return null;
  final header = (data[pos] << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3];
  final version = (header >> 19) & 0x03;
  final layer = (header >> 17) & 0x03;
  final bitrateIndex = (header >> 12) & 0x0f;
  final rateIndex = (header >> 10) & 0x03;
  final padding = (header >> 9) & 0x01;
  if (version == 1 || layer == 0 || bitrateIndex == 0 || bitrateIndex == 15 || rateIndex == 3) {
    return null;
  }
  final brRow = _bitrates['$version,$layer'];
  final rates = _sampleRates[version];
  if (brRow == null || rates == null) return null;
  final bitrate = brRow[bitrateIndex];
  final sampleRate = rates[rateIndex];
  if (bitrate == 0 || sampleRate == 0) return null;
  int length;
  int samples;
  if (layer == 3) {
    length = (12 * bitrate * 1000 ~/ sampleRate + padding) * 4;
    samples = 384;
  } else if (layer == 2) {
    length = 144 * bitrate * 1000 ~/ sampleRate + padding;
    samples = 1152;
  } else {
    samples = version == 3 ? 1152 : 576;
    length = (samples ~/ 8) * bitrate * 1000 ~/ sampleRate + padding;
  }
  if (length <= 4) return null;
  return [length, samples, bitrate, sampleRate];
}

/// 走完整条 MP3 帧链，返回 (帧数, 总采样数, 采样率, 码率)。
///
/// MP3 文件本身不大（几十 MB 以内），直接读进内存线性走帧，
/// 避免分块读取时反复拷贝缓冲导致 O(n²)。
List<int> mp3Measure(String path) {
  final raf = File(path).openSync();
  Uint8List data;
  try {
    final head = raf.readSync(10);
    final start = id3TagSize(head);
    final length = raf.lengthSync();
    raf.setPositionSync(start);
    data = raf.readSync(length > start ? length - start : 0);
  } finally {
    raf.closeSync();
  }

  var pos = 0;
  var frames = 0, samples = 0, sampleRate = 0, bitrate = 0;
  while (pos + 4 <= data.length && frames < 4000000) {
    final parsed = _mp3Frame(data, pos);
    if (parsed == null) {
      if (frames > 0) break;
      pos++;
      continue;
    }
    frames++;
    samples += parsed[1];
    bitrate = parsed[2];
    sampleRate = parsed[3];
    pos += parsed[0];
  }
  return [frames, samples, sampleRate, bitrate];
}

class FlacInfo {
  final int sampleRate, channels, bits, samples, durationMs, audioOffset;
  final bool frameSyncOk;
  FlacInfo(this.sampleRate, this.channels, this.bits, this.samples, this.durationMs,
      this.audioOffset, this.frameSyncOk);
}

FlacInfo flacStreamInfo(String path) {
  final raf = File(path).openSync();
  try {
    final (blocks, audioOffset) = parseFlacMetadata(raf);
    Uint8List? streamInfo;
    for (final block in blocks) {
      if (block[0] == 0) {
        streamInfo = block[1] as Uint8List;
        break;
      }
    }
    if (streamInfo == null || streamInfo.length < 18) {
      throw const FormatException('缺少 STREAMINFO 块');
    }
    final packed = _be64(streamInfo, 10);
    final sampleRate = (packed >> 44) & 0xfffff;
    final channels = ((packed >> 41) & 0x07) + 1;
    final bits = ((packed >> 36) & 0x1f) + 1;
    final samples = packed & ((1 << 36) - 1);
    final sync = raf.readSync(2);
    final ok = sync.length == 2 && sync[0] == 0xff && (sync[1] & 0xfe) == 0xf8;
    return FlacInfo(sampleRate, channels, bits, samples,
        sampleRate > 0 ? (samples * 1000 ~/ sampleRate) : 0, audioOffset, ok);
  } finally {
    raf.closeSync();
  }
}

int _be64(Uint8List data, int offset) {
  var value = 0;
  for (var i = 0; i < 8; i++) {
    value = (value << 8) | data[offset + i];
  }
  return value;
}

int _crc8(Uint8List data, int start, int end) {
  var crc = 0;
  for (var i = start; i < end; i++) {
    crc ^= data[i];
    for (var b = 0; b < 8; b++) {
      crc = (crc & 0x80) != 0 ? ((crc << 1) ^ 0x07) & 0xff : (crc << 1) & 0xff;
    }
  }
  return crc;
}

const Map<int, int> _flacBlocksizeCode = {
  1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608,
  8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192, 14: 16384, 15: 32768,
};

/// 解析 FLAC 帧头，返回 (采样数, 帧头长度) 或 null。
List<int>? _flacFrameHeader(Uint8List data, int pos, int channels) {
  if (pos + 5 > data.length) return null;
  if (data[pos] != 0xff || (data[pos + 1] & 0xfe) != 0xf8 || (data[pos + 1] & 0x02) != 0) return null;
  final blocksizeCode = data[pos + 2] >> 4;
  final sampleRateCode = data[pos + 2] & 0x0f;
  final channelCode = data[pos + 3] >> 4;
  final sampleSizeCode = (data[pos + 3] >> 1) & 0x07;
  if (data[pos + 3] & 0x01 != 0) return null;
  if (blocksizeCode == 0 || sampleRateCode == 15 || sampleSizeCode == 3) return null;
  if (channelCode > 10) return null;
  if (channels > 0) {
    if (channelCode < 8) {
      if (channelCode + 1 != channels) return null;
    } else if (channels != 2) {
      return null;
    }
  }
  var index = pos + 4;
  final first = data[index];
  if (first < 0x80) {
    index += 1;
  } else {
    var mask = 0x80, count = 0;
    while (first & mask != 0) {
      count++;
      mask >>= 1;
    }
    if (count < 2 || count > 7) return null;
    index += count;
  }
  if (index + 2 > data.length) return null;
  int? blocksize;
  if (blocksizeCode == 6) {
    blocksize = data[index] + 1;
    index += 1;
  } else if (blocksizeCode == 7) {
    blocksize = ((data[index] << 8) | data[index + 1]) + 1;
    index += 2;
  }
  if (sampleRateCode == 12) {
    index += 1;
  } else if (sampleRateCode == 13 || sampleRateCode == 14) {
    index += 2;
  }
  if (index >= data.length) return null;
  if (_crc8(data, pos, index) != data[index]) return null;
  blocksize ??= _flacBlocksizeCode[blocksizeCode];
  if (blocksize == null) return null;
  return [blocksize, index + 1 - pos];
}

/// 统计 FLAC 音频区实际帧数与总采样数（判断源文件是否被截断）。
List<int> flacFrameMeasure(String path, int audioOffset, int channels) {
  final raf = File(path).openSync();
  try {
    raf.setPositionSync(audioOffset);
    var frames = 0, samples = 0;
    var tail = Uint8List(0);
    while (true) {
      final chunk = raf.readSync(8 << 20);
      if (chunk.isEmpty) break;
      final data = Uint8List(tail.length + chunk.length)
        ..setRange(0, tail.length, tail)
        ..setRange(tail.length, tail.length + chunk.length, chunk);
      var pos = 0;
      while (true) {
        var index = -1;
        for (var i = pos; i < data.length; i++) {
          if (data[i] == 0xff) {
            index = i;
            break;
          }
        }
        if (index < 0 || index + 16 > data.length) break;
        final parsed = _flacFrameHeader(data, index, channels);
        if (parsed == null) {
          pos = index + 1;
          continue;
        }
        frames++;
        samples += parsed[0];
        pos = index + parsed[1];
      }
      tail = data.length > 32 ? Uint8List.sublistView(data, data.length - 32) : data;
    }
    return [frames, samples];
  } finally {
    raf.closeSync();
  }
}

// --------------------------------------------------------------------------- //
// 转换器
// --------------------------------------------------------------------------- //

class Converter {
  final ConvertOptions options;
  bool _canceled = false;

  Converter(this.options);

  void cancel() => _canceled = true;
  bool get canceled => _canceled;

  Map<String, String> tagInfo(NcmSong song, SourceItem item) => {
        'title': song.musicName.isEmpty ? item.stem : song.musicName,
        'artist': song.artistText,
        'album_artist': song.artistText,
        'album': song.album,
        'track': (song.meta['track'] ?? '').toString(),
        'year': (song.meta['year'] ?? '').toString(),
        'encoder': kEncoderTag,
      };

  String verifyOutput(String dst, String fmt, NcmSong song, SourceItem item) {
    final notes = <String>[];
    final warnings = <String>[];
    try {
      if (fmt == 'flac') {
        final info = flacStreamInfo(dst);
        final declared = info.durationMs;
        final expect = song.durationMs;
        if (expect > 0 && declared > 0 && (declared - expect).abs() > 2000) {
          warnings.add('时长与元数据相差 ${((declared - expect).abs() / 1000).toStringAsFixed(1)}s');
        }
        if (!info.frameSyncOk) warnings.add('首个音频帧同步码异常');
        final measured = flacFrameMeasure(dst, info.audioOffset, info.channels);
        final measuredMs = info.sampleRate > 0 ? measured[1] * 1000 ~/ info.sampleRate : 0;
        if (measured[0] < 8) {
          warnings.add('音频帧数异常（只找到 ${measured[0]} 帧）');
        } else if (info.samples > 0 && measured[1] > 0) {
          final ratio = measured[1] / info.samples;
          if (ratio < 0.98) {
            warnings.add('音频数据不完整：实际只覆盖约 ${(ratio * 100).toStringAsFixed(0)}%'
                '（${humanDuration(measuredMs)} / ${humanDuration(declared)}），源文件很可能没有下载完');
          }
        }
        notes.add('${info.sampleRate}Hz/${info.channels}声道/${info.bits}bit '
            '${humanDuration(measuredMs > 0 ? measuredMs : declared)}（${measured[0]} 帧）');
        if (options.writeTags && readFlacTags(dst)['TITLE'] != (song.musicName.isEmpty ? item.stem : song.musicName)) {
          warnings.add('标签写入后读回不一致');
        }
      } else if (fmt == 'mp3') {
        final m = mp3Measure(dst);
        if (m[0] < 2) warnings.add('MP3 帧头连续扫描失败');
        final duration = m[2] > 0 ? m[1] * 1000 ~/ m[2] : 0;
        notes.add('${m[0]} 帧 ${m[2]}Hz ${m[3]}kbps ${humanDuration(duration)}');
        final expect = song.durationMs;
        if (expect > 0 && duration > 0) {
          final delta = (duration - expect).abs();
          if (delta > (expect * 0.02 > 3000 ? expect * 0.02 : 3000)) {
            warnings.add('实际时长 ${humanDuration(duration)} 与元数据 ${humanDuration(expect)} 不符，'
                '源文件很可能没有下载完');
          }
        }
        if (options.writeTags && readId3v23(dst)['TIT2'] != (song.musicName.isEmpty ? item.stem : song.musicName)) {
          warnings.add('标签写入后读回不一致');
        }
      }
    } catch (exc) {
      warnings.add('校验异常：$exc');
    }
    if (warnings.isNotEmpty) return '⚠ ${warnings.join('；')}';
    return notes.join('；');
  }

  FileResult convertOne(
    SourceItem item, {
    void Function(double)? onProgress,
  }) {
    final started = DateTime.now();
    double elapsed() => DateTime.now().difference(started).inMilliseconds / 1000.0;

    FileResult fail(String message) =>
        FileResult(item.path, '', '', 'failed', message, elapsed(), 0);

    if (item.error.isNotEmpty) return fail('解析失败：${item.error}');

    NcmSong song;
    try {
      song = parseNcm(item.path);
    } catch (exc) {
      return fail('解析失败：$exc');
    }

    final fmt = song.audioFormat.toLowerCase();
    final dst = _uniquePath(expectedOutputPath(item, options));
    if (!options.overwrite) {
      final existing = findExistingAudio(expectedOutputPath(item, options));
      if (existing != null) {
        return FileResult(item.path, existing, fmt, 'skipped',
            '已有转换结果（${existing.split(RegExp(r'[\\/]')).last}），已跳过', elapsed(),
            File(existing).lengthSync());
      }
    }

    final tmp = '$dst.part';
    try {
      Directory(File(dst).parent.path).createSync(recursive: true);
      final tmpFile = File(tmp);
      if (tmpFile.existsSync()) tmpFile.deleteSync();
      decryptAudioToFile(
        song,
        tmp,
        onProgress: (done, total) => onProgress?.call(total > 0 ? done / total : 0),
        shouldStop: () => _canceled,
      );
      if (_canceled) {
        if (tmpFile.existsSync()) tmpFile.deleteSync();
        return FileResult(item.path, dst, fmt, 'canceled', '已取消', elapsed(), 0);
      }
      if (options.writeTags) {
        final info = tagInfo(song, item);
        if (fmt == 'mp3') {
          writeId3v23(tmp, info, song.cover, song.coverMime);
        } else if (fmt == 'flac') {
          writeFlacTags(tmp, info, song.cover, song.coverMime);
        }
      }
      _replaceSync(tmp, dst);

      var message = '';
      if (options.verify) message = verifyOutput(dst, fmt, song, item);

      if (options.deleteSource && !message.startsWith('⚠')) {
        try {
          File(item.path).deleteSync();
        } catch (_) {}
      } else if (options.deleteSource) {
        message = '$message；输出未通过校验，已保留原 ncm';
      }
      onProgress?.call(1);
      return FileResult(item.path, dst, fmt, 'ok', message, elapsed(), File(dst).lengthSync());
    } catch (exc) {
      final tmpFile = File(tmp);
      if (tmpFile.existsSync()) {
        try {
          tmpFile.deleteSync();
        } catch (_) {}
      }
      return fail('转换失败：$exc');
    }
  }

  final Set<String> _reserved = {};

  String _uniquePath(String path) {
    if (_reserved.add(path.toLowerCase())) return path;
    final dot = path.lastIndexOf('.');
    final base = dot > 0 ? path.substring(0, dot) : path;
    final ext = dot > 0 ? path.substring(dot) : '';
    var index = 2;
    while (true) {
      final candidate = '$base ($index)$ext';
      if (_reserved.add(candidate.toLowerCase())) return candidate;
      index++;
    }
  }

  void _replaceSync(String temp, String path) {
    try {
      File(temp).renameSync(path);
    } on FileSystemException {
      final target = File(path);
      if (target.existsSync()) target.deleteSync();
      File(temp).renameSync(path);
    }
  }

  List<FileResult> run(
    List<SourceItem> items, {
    void Function(int index, SourceItem item)? onStart,
    void Function(int index, double progress)? onProgress,
    void Function(int index, FileResult result)? onDone,
    void Function(String message)? log,
  }) {
    final results = <FileResult>[];
    for (var i = 0; i < items.length; i++) {
      if (_canceled) {
        final canceled = FileResult(items[i].path, '', '', 'canceled', '已取消', 0, 0);
        results.add(canceled);
        onDone?.call(i, canceled);
        continue;
      }
      onStart?.call(i, items[i]);
      log?.call('开始转换：${items[i].name}');
      final result = convertOne(items[i], onProgress: (p) => onProgress?.call(i, p));
      results.add(result);
      onDone?.call(i, result);
      log?.call(result.status == 'ok'
          ? '✔ ${items[i].name} → ${result.dst.split(RegExp(r'[\\/]')).last}'
              '${result.message.isEmpty ? '' : '  [${result.message}]'}'
          : result.status == 'skipped'
              ? '↷ 跳过：${items[i].name}（${result.message}）'
              : result.status == 'canceled'
                  ? '✖ 已取消：${items[i].name}'
                  : '✘ 失败：${items[i].name}（${result.message}）');
    }
    return results;
  }
}
