// 标签与封面写入：MP3 用 ID3v2.3，FLAC 用 Vorbis Comment + PICTURE。
// 与 Windows 版 tags.py 同一套写法，先写临时文件再替换，不破坏已解密好的音频。

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

// --------------------------------------------------------------------------- //
// 公共工具
// --------------------------------------------------------------------------- //

Uint8List _u32be(int value) => Uint8List.fromList([
      (value >> 24) & 0xff, (value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff,
    ]);

Uint8List _u32le(int value) => Uint8List.fromList([
      value & 0xff, (value >> 8) & 0xff, (value >> 16) & 0xff, (value >> 24) & 0xff,
    ]);

Uint8List _syncSafe(int value) => Uint8List.fromList([
      (value >> 21) & 0x7f, (value >> 14) & 0x7f, (value >> 7) & 0x7f, value & 0x7f,
    ]);

Uint8List _concat(List<List<int>> parts) {
  var total = 0;
  for (final part in parts) {
    total += part.length;
  }
  final out = Uint8List(total);
  var offset = 0;
  for (final part in parts) {
    out.setRange(offset, offset + part.length, part);
    offset += part.length;
  }
  return out;
}

void _replaceFile(String temp, String path) {
  final target = File(path);
  try {
    File(temp).renameSync(path);
  } on FileSystemException {
    if (target.existsSync()) target.deleteSync();
    File(temp).renameSync(path);
  }
}

void _discard(String path) {
  try {
    final file = File(path);
    if (file.existsSync()) file.deleteSync();
  } catch (_) {}
}

/// 把歌曲信息清理成合法文件名。
String sanitizeFileName(String name, {int maxLen = 150}) {
  var out = name;
  for (final ch in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']) {
    out = out.replaceAll(ch, '_');
  }
  out = out.replaceAll(RegExp(r'[\x00-\x1f]'), '');
  out = out.trim();
  while (out.endsWith('.')) {
    out = out.substring(0, out.length - 1);
  }
  if (out.length > maxLen) out = out.substring(0, maxLen).trim();
  return out.isEmpty ? 'untitled' : out;
}

// --------------------------------------------------------------------------- //
// ID3v2.3（MP3）
// --------------------------------------------------------------------------- //

Uint8List _id3TextFrame(String frameId, String text) {
  if (text.isEmpty) return Uint8List(0);
  List<int> body;
  final isLatin = text.codeUnits.every((c) => c < 256);
  if (isLatin) {
    body = [0x00, ...text.codeUnits];
  } else {
    // UTF-16LE + BOM
    final units = <int>[0x01, 0xff, 0xfe];
    for (final rune in text.runes) {
      if (rune > 0xffff) {
        final v = rune - 0x10000;
        units.addAll([(v >> 10) + 0xd800 & 0xff, ((v >> 10) + 0xd800) >> 8]);
        units.addAll([(v & 0x3ff) + 0xdc00 & 0xff, ((v & 0x3ff) + 0xdc00) >> 8]);
      } else {
        units.addAll([rune & 0xff, (rune >> 8) & 0xff]);
      }
    }
    body = units;
  }
  return _concat([
    frameId.codeUnits,
    _u32be(body.length),
    [0x00, 0x00],
    body,
  ]);
}

Uint8List _id3ApicFrame(String mime, Uint8List image) {
  if (image.isEmpty) return Uint8List(0);
  final body = _concat([
    [0x00],
    mime.codeUnits,
    [0x00],
    [0x03], // 封面
    [0x00], // 空描述
    image,
  ]);
  return _concat([
    'APIC'.codeUnits,
    _u32be(body.length),
    [0x00, 0x00],
    body,
  ]);
}

Uint8List buildId3v23Tag(Map<String, String> info, Uint8List cover, String mime) {
  final frames = _concat([
    _id3TextFrame('TIT2', info['title'] ?? ''),
    _id3TextFrame('TPE1', info['artist'] ?? ''),
    _id3TextFrame('TPE2', (info['album_artist'] ?? '').isEmpty ? (info['artist'] ?? '') : info['album_artist']!),
    _id3TextFrame('TALB', info['album'] ?? ''),
    _id3TextFrame('TRCK', info['track'] ?? ''),
    _id3TextFrame('TYER', info['year'] ?? ''),
    _id3TextFrame('TENC', info['encoder'] ?? ''),
    _id3ApicFrame(mime, cover),
  ]);
  return _concat([
    'ID3'.codeUnits,
    [0x03, 0x00, 0x00],
    _syncSafe(frames.length),
    frames,
  ]);
}

int id3TagSize(Uint8List head) {
  if (head.length < 10 || head[0] != 0x49 || head[1] != 0x44 || head[2] != 0x33) return 0;
  var size = 0;
  for (var i = 6; i < 10; i++) {
    size = (size << 7) | (head[i] & 0x7f);
  }
  var total = size + 10;
  if (head[5] & 0x10 != 0) total += 10;
  return total;
}

void writeId3v23(String path, Map<String, String> info, Uint8List cover, String mime) {
  final tag = buildId3v23Tag(info, cover, mime);
  final temp = '$path.tags.tmp';
  try {
    final src = File(path).openSync();
    final dst = File(temp).openSync(mode: FileMode.write);
    try {
      final head = src.readSync(10);
      final old = id3TagSize(head);
      if (old > 0) src.setPositionSync(old);
      dst.writeFromSync(tag);
      while (true) {
        final chunk = src.readSync(1 << 20);
        if (chunk.isEmpty) break;
        dst.writeFromSync(chunk);
      }
    } finally {
      src.closeSync();
      dst.closeSync();
    }
    _replaceFile(temp, path);
  } catch (_) {
    _discard(temp);
    rethrow;
  }
}

Map<String, String> readId3v23(String path) {
  final out = <String, String>{};
  final raf = File(path).openSync();
  try {
    final head = raf.readSync(10);
    if (head.length < 10 || head[0] != 0x49 || head[1] != 0x44 || head[2] != 0x33) return out;
    final isV24 = head[3] == 4;
    var size = 0;
    for (var i = 6; i < 10; i++) {
      size = (size << 7) | (head[i] & 0x7f);
    }
    final data = raf.readSync(size);
    var pos = 0;
    while (pos + 10 <= data.length) {
      final frameId = String.fromCharCodes(data.sublist(pos, pos + 4));
      if (frameId.trim().isEmpty) break;
      var len = 0;
      if (isV24) {
        for (var i = pos + 4; i < pos + 8; i++) {
          len = (len << 7) | (data[i] & 0x7f);
        }
      } else {
        len = (data[pos + 4] << 24) | (data[pos + 5] << 16) | (data[pos + 6] << 8) | data[pos + 7];
      }
      if (len <= 0 || pos + 10 + len > data.length) break;
      final body = data.sublist(pos + 10, pos + 10 + len);
      pos += 10 + len;
      if (!frameId.startsWith('T') || body.isEmpty) continue;
      final enc = body[0];
      try {
        String text;
        if (enc == 0) {
          text = String.fromCharCodes(body.sublist(1));
        } else if (enc == 1) {
          final units = <int>[];
          for (var i = 1; i + 1 < body.length; i += 2) {
            units.add(body[i] | (body[i + 1] << 8));
          }
          text = String.fromCharCodes(units);
        } else {
          text = String.fromCharCodes(body.sublist(1));
        }
        out[frameId] = text.replaceAll('\u0000', '').replaceFirst('\uFEFF', '');
      } catch (_) {
        continue;
      }
    }
  } finally {
    raf.closeSync();
  }
  return out;
}

// --------------------------------------------------------------------------- //
// FLAC
// --------------------------------------------------------------------------- //

Uint8List _vorbisCommentBlock(List<List<String>> fields) {
  // Vorbis Comment 规定用 UTF-8，这里必须 utf8.encode：
  // 用 codeUnits 会把中文按 UTF-16 截断成乱码。
  final vendor = utf8.encode('ncm2mp3');
  final parts = <List<int>>[_u32le(vendor.length), vendor, _u32le(fields.length)];
  for (final field in fields) {
    final item = utf8.encode('${field[0]}=${field[1]}');
    parts.add(_u32le(item.length));
    parts.add(item);
  }
  return _concat(parts);
}

Uint8List _flacPictureBlock(String mime, Uint8List image) {
  final m = mime.codeUnits;
  return _concat([
    _u32be(3), // 封面
    _u32be(m.length), m,
    _u32be(0), // 空描述
    _u32be(0), _u32be(0), _u32be(0), _u32be(0), // 宽/高/色深/调色板
    _u32be(image.length), image,
  ]);
}

/// 读取 FLAC 全部元数据块，返回 (块列表, 音频起始偏移)。
(List<List<dynamic>>, int) parseFlacMetadata(RandomAccessFile raf) {
  final magic = raf.readSync(4);
  if (magic.length < 4 || String.fromCharCodes(magic) != 'fLaC') {
    throw const FormatException('不是 FLAC 文件');
  }
  final blocks = <List<dynamic>>[];
  while (true) {
    final header = raf.readSync(4);
    if (header.length < 4) throw const FormatException('FLAC 元数据块不完整');
    final isLast = (header[0] & 0x80) != 0;
    final type = header[0] & 0x7f;
    final length = (header[1] << 16) | (header[2] << 8) | header[3];
    final data = raf.readSync(length);
    if (data.length != length) throw const FormatException('FLAC 元数据块长度与内容不符');
    blocks.add([type, data]);
    if (isLast) break;
  }
  return (blocks, raf.positionSync());
}

void writeFlacTags(String path, Map<String, String> info, Uint8List cover, String mime) {
  final temp = '$path.tags.tmp';
  try {
    final src = File(path).openSync();
    final dst = File(temp).openSync(mode: FileMode.write);
    try {
      final (blocks, audioStart) = parseFlacMetadata(src);
      final kept = blocks.where((b) => b[0] != 4 && b[0] != 6).toList();
      if (kept.isEmpty || kept[0][0] != 0) {
        throw const FormatException('FLAC 缺少 STREAMINFO 块');
      }
      final fields = <List<String>>[];
      for (final entry in [
        ['TITLE', 'title'],
        ['ARTIST', 'artist'],
        ['ALBUM', 'album'],
        ['ALBUMARTIST', 'album_artist'],
        ['TRACKNUMBER', 'track'],
        ['DATE', 'year'],
        ['ENCODER', 'encoder'],
      ]) {
        final value = info[entry[1]] ?? '';
        if (value.isNotEmpty) fields.add([entry[0], value]);
      }
      final newBlocks = <List<dynamic>>[
        ...kept,
        [4, _vorbisCommentBlock(fields)],
        if (cover.isNotEmpty) [6, _flacPictureBlock(mime, cover)],
      ];
      dst.writeFromSync('fLaC'.codeUnits);
      for (var i = 0; i < newBlocks.length; i++) {
        final type = newBlocks[i][0] as int;
        final data = newBlocks[i][1] as Uint8List;
        final last = (i == newBlocks.length - 1) ? 0x80 : 0x00;
        dst.writeFromSync([last | type, (data.length >> 16) & 0xff, (data.length >> 8) & 0xff, data.length & 0xff]);
        dst.writeFromSync(data);
      }
      src.setPositionSync(audioStart);
      while (true) {
        final chunk = src.readSync(1 << 20);
        if (chunk.isEmpty) break;
        dst.writeFromSync(chunk);
      }
    } finally {
      src.closeSync();
      dst.closeSync();
    }
    _replaceFile(temp, path);
  } catch (_) {
    _discard(temp);
    rethrow;
  }
}

Map<String, String> readFlacTags(String path) {
  final out = <String, String>{};
  final raf = File(path).openSync();
  try {
    final (blocks, _) = parseFlacMetadata(raf);
    for (final block in blocks) {
      if (block[0] != 4) continue;
      final data = block[1] as Uint8List;
      var pos = 0;
      final vendorLen = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16) | (data[pos + 3] << 24);
      pos += 4 + vendorLen;
      final count = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16) | (data[pos + 3] << 24);
      pos += 4;
      for (var i = 0; i < count && pos + 4 <= data.length; i++) {
        final len = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16) | (data[pos + 3] << 24);
        pos += 4;
        final item = utf8.decode(data.sublist(pos, pos + len), allowMalformed: true);
        pos += len;
        final eq = item.indexOf('=');
        if (eq > 0) out[item.substring(0, eq).toUpperCase()] = item.substring(eq + 1);
      }
    }
  } finally {
    raf.closeSync();
  }
  return out;
}
