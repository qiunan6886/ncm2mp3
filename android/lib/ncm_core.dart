// NCM 解密核心（纯 Dart，不依赖 Flutter，可在桌面 Dart VM 上直接跑测试）
//
// 与 Windows 版（Python）保持同一套算法：
//   magic 8 字节 -> 2 字节占位 -> 密钥块(AES-128-ECB 去 PKCS#7 填充, 去前 17 字节)
//   -> 元数据块(XOR 0x63 + base64 + AES 解密 + 去 "music:") -> CRC32 -> 图片版本
//   -> 封面帧长度 -> 封面长度 -> 封面数据 -> 音频正文(密钥流逐字节异或)
//
// 音频正文不重新编码：里面是 mp3 就是 mp3，是 flac 就是 flac。

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

class NcmException implements Exception {
  final String message;
  NcmException(this.message);
  @override
  String toString() => message;
}

const List<int> kCoreKey = [
  0x68, 0x7A, 0x48, 0x52, 0x41, 0x6D, 0x73, 0x6F,
  0x35, 0x6B, 0x49, 0x6E, 0x62, 0x61, 0x78, 0x57,
];

const List<int> kMetaKey = [
  0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21,
  0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28,
];

const int kReadChunk = 1 << 20; // 必须是 256 的整数倍，保证密钥流对齐

// --------------------------------------------------------------------------- //
// AES-128 (ECB)
// --------------------------------------------------------------------------- //

const List<int> _sboxValues = [
  0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
  0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
  0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
  0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
  0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
  0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
  0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
  0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
  0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
  0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
  0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
  0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
  0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
  0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
  0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
  0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
];

final Uint8List _sbox = Uint8List.fromList(_sboxValues);
final Uint8List _invSbox = () {
  final inv = Uint8List(256);
  for (var i = 0; i < 256; i++) {
    inv[_sbox[i]] = i;
  }
  return inv;
}();

const List<int> _rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36];

int _gfMul(int a, int b) {
  var p = 0;
  var x = a;
  var y = b;
  for (var i = 0; i < 8; i++) {
    if (y & 1 != 0) p ^= x;
    final hi = x & 0x80;
    x = (x << 1) & 0xff;
    if (hi != 0) x ^= 0x1b;
    y >>= 1;
  }
  return p;
}

class Aes128 {
  final List<List<int>> _w;

  Aes128(List<int> key) : _w = _expand(key);

  static List<List<int>> _expand(List<int> key) {
    if (key.length != 16) throw NcmException('AES-128 需要 16 字节密钥');
    final w = <List<int>>[];
    for (var i = 0; i < 4; i++) {
      w.add(key.sublist(i * 4, i * 4 + 4));
    }
    for (var i = 4; i < 44; i++) {
      var temp = List<int>.from(w[i - 1]);
      if (i % 4 == 0) {
        temp = [temp[1], temp[2], temp[3], temp[0]];
        temp = [for (final b in temp) _sbox[b]];
        temp[0] ^= _rcon[i ~/ 4 - 1];
      }
      w.add([for (var j = 0; j < 4; j++) w[i - 4][j] ^ temp[j]]);
    }
    return w;
  }

  static List<List<int>> _toState(Uint8List block, int offset) {
    final state = List.generate(4, (_) => List<int>.filled(4, 0));
    for (var r = 0; r < 4; r++) {
      for (var c = 0; c < 4; c++) {
        state[r][c] = block[offset + r + 4 * c];
      }
    }
    return state;
  }

  static void _fromState(List<List<int>> state, Uint8List out, int offset) {
    for (var c = 0; c < 4; c++) {
      for (var r = 0; r < 4; r++) {
        out[offset + r + 4 * c] = state[r][c];
      }
    }
  }

  void _addRoundKey(List<List<int>> state, int round) {
    for (var c = 0; c < 4; c++) {
      final word = _w[round * 4 + c];
      for (var r = 0; r < 4; r++) {
        state[r][c] ^= word[r];
      }
    }
  }

  Uint8List decryptEcb(Uint8List data) {
    final out = Uint8List(data.length);
    for (var off = 0; off < data.length; off += 16) {
      final state = _toState(data, off);
      _addRoundKey(state, 10);
      for (var round = 9; round >= 1; round--) {
        for (var r = 1; r < 4; r++) {
          state[r] = [...state[r].sublist(4 - r), ...state[r].sublist(0, 4 - r)];
        }
        for (var r = 0; r < 4; r++) {
          for (var c = 0; c < 4; c++) {
            state[r][c] = _invSbox[state[r][c]];
          }
        }
        _addRoundKey(state, round);
        for (var c = 0; c < 4; c++) {
          final a = [state[0][c], state[1][c], state[2][c], state[3][c]];
          state[0][c] = _gfMul(a[0], 14) ^ _gfMul(a[1], 11) ^ _gfMul(a[2], 13) ^ _gfMul(a[3], 9);
          state[1][c] = _gfMul(a[0], 9) ^ _gfMul(a[1], 14) ^ _gfMul(a[2], 11) ^ _gfMul(a[3], 13);
          state[2][c] = _gfMul(a[0], 13) ^ _gfMul(a[1], 9) ^ _gfMul(a[2], 14) ^ _gfMul(a[3], 11);
          state[3][c] = _gfMul(a[0], 11) ^ _gfMul(a[1], 13) ^ _gfMul(a[2], 9) ^ _gfMul(a[3], 14);
        }
      }
      for (var r = 1; r < 4; r++) {
        state[r] = [...state[r].sublist(4 - r), ...state[r].sublist(0, 4 - r)];
      }
      for (var r = 0; r < 4; r++) {
        for (var c = 0; c < 4; c++) {
          state[r][c] = _invSbox[state[r][c]];
        }
      }
      _addRoundKey(state, 0);
      _fromState(state, out, off);
    }
    return out;
  }
}

/// 解密并按官方实现去掉末块的 PKCS#7 填充。
Uint8List aesEcbDecryptUnpad(Uint8List data, List<int> key) {
  var end = data.length - (data.length % 16);
  if (end <= 0) return Uint8List(0);
  final plain = Aes128(key).decryptEcb(Uint8List.sublistView(data, 0, end));
  var pad = plain[plain.length - 1];
  if (pad > 16) pad = 0;
  if (pad > 0 && pad <= plain.length) {
    return Uint8List.sublistView(plain, 0, plain.length - pad);
  }
  return plain;
}

Uint8List pkcs7Unpad(Uint8List data) {
  if (data.isEmpty) return data;
  final pad = data[data.length - 1];
  if (pad >= 1 && pad <= 16 && data.length >= pad) {
    var ok = true;
    for (var i = data.length - pad; i < data.length; i++) {
      if (data[i] != pad) {
        ok = false;
        break;
      }
    }
    if (ok) return Uint8List.sublistView(data, 0, data.length - pad);
  }
  return data;
}

// --------------------------------------------------------------------------- //
// 密钥盒 / 密钥流
// --------------------------------------------------------------------------- //

Uint8List buildKeyBox(List<int> key) {
  final box = Uint8List(256);
  for (var i = 0; i < 256; i++) {
    box[i] = i;
  }
  final keyLen = key.length;
  if (keyLen == 0) throw NcmException('NCM 密钥为空');
  var c = 0;
  var last = 0;
  var off = 0;
  for (var i = 0; i < 256; i++) {
    final swap = box[i];
    c = (swap + last + key[off]) & 0xff;
    off++;
    if (off >= keyLen) off = 0;
    box[i] = box[c];
    box[c] = swap;
    last = c;
  }
  return box;
}

Uint8List buildKeystream(Uint8List keyBox) {
  final ks = Uint8List(256);
  for (var i = 0; i < 256; i++) {
    final j = (i + 1) & 0xff;
    ks[i] = keyBox[(keyBox[j] + keyBox[(keyBox[j] + j) & 0xff]) & 0xff];
  }
  return ks;
}

Uint8List xorWithKeystream(Uint8List data, Uint8List keystream) {
  final out = Uint8List(data.length);
  for (var i = 0; i < data.length; i++) {
    out[i] = data[i] ^ keystream[i & 0xff];
  }
  return out;
}

// --------------------------------------------------------------------------- //
// 解析
// --------------------------------------------------------------------------- //

class NcmSong {
  final String path;
  final int size;
  final int audioOffset;
  final int audioSize;
  final Uint8List keyBox;
  final Uint8List cover;
  final Map<String, dynamic> meta;
  final Uint8List rawMagic;

  NcmSong({
    required this.path,
    required this.size,
    required this.audioOffset,
    required this.audioSize,
    required this.keyBox,
    required this.cover,
    required this.meta,
    required this.rawMagic,
  });

  String get musicName => (meta['musicName'] ?? '').toString();
  String get album => (meta['album'] ?? '').toString();

  List<String> get artists {
    final out = <String>[];
    final raw = meta['artist'];
    if (raw is List) {
      for (final item in raw) {
        if (item is List && item.isNotEmpty) {
          out.add(item[0].toString());
        } else if (item is String) {
          out.add(item);
        }
      }
    }
    return out;
  }

  String get artistText => artists.join(' / ');

  int get durationMs => int.tryParse((meta['duration'] ?? 0).toString()) ?? 0;
  int get bitrate => int.tryParse((meta['bitrate'] ?? 0).toString()) ?? 0;
  String get declaredFormat => (meta['format'] ?? '').toString().toLowerCase();

  /// 按解密后的真实文件头判断格式，比元数据可靠。
  String get audioFormat {
    if (rawMagic.length >= 4) {
      final head = String.fromCharCodes(rawMagic.sublist(0, 4));
      if (head == 'fLaC') return 'flac';
      if (head == 'OggS') return 'ogg';
      if (head == 'RIFF') return 'wav';
    }
    if (rawMagic.length >= 3 && String.fromCharCodes(rawMagic.sublist(0, 3)) == 'ID3') {
      return 'mp3';
    }
    if (rawMagic.length >= 2 && rawMagic[0] == 0xff && (rawMagic[1] & 0xE0) == 0xE0) {
      return 'mp3';
    }
    return declaredFormat.isEmpty ? 'mp3' : declaredFormat;
  }

  String get coverMime {
    if (cover.length >= 8 &&
        cover[0] == 0x89 && cover[1] == 0x50 && cover[2] == 0x4e && cover[3] == 0x47) {
      return 'image/png';
    }
    return 'image/jpeg';
  }

  String get coverExt => coverMime == 'image/png' ? '.png' : '.jpg';

  String displayName() {
    final name = musicName.isEmpty ? _stem(path) : musicName;
    if (artistText.isEmpty) return name;
    return '$artistText - $name';
  }
}

String _stem(String path) {
  final name = path.split(RegExp(r'[\\/]')).last;
  final dot = name.lastIndexOf('.');
  return dot > 0 ? name.substring(0, dot) : name;
}

Uint8List _readExact(RandomAccessFile raf, int count) {
  final data = raf.readSync(count);
  if (data.length != count) {
    throw NcmException('文件在读取时提前结束（期望 $count 字节，实际 ${data.length} 字节）');
  }
  return data;
}

int _u32(Uint8List data, [int offset = 0]) =>
    data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) | (data[offset + 3] << 24);

bool _startsWith(Uint8List data, String text) {
  if (data.length < text.length) return false;
  for (var i = 0; i < text.length; i++) {
    if (data[i] != text.codeUnitAt(i)) return false;
  }
  return true;
}

Map<String, dynamic>? decryptMeta(Uint8List blob) {
  if (blob.isEmpty) return null;
  final body = Uint8List(blob.length);
  for (var i = 0; i < blob.length; i++) {
    body[i] = blob[i] ^ 0x63;
  }
  var start = 0;
  if (body.length >= 22 && _startsWith(body, "163 key(Don't modify):")) {
    start = 22;
  }
  try {
    final decoded = base64.decode(String.fromCharCodes(body.sublist(start)));
    final plain = aesEcbDecryptUnpad(Uint8List.fromList(decoded), kMetaKey);
    var text = utf8.decode(plain, allowMalformed: true);
    if (text.startsWith('music:')) text = text.substring(6);
    final candidates = <String>[text, utf8.decode(pkcs7Unpad(plain), allowMalformed: true)];
    final end = text.lastIndexOf('}');
    if (end != -1) candidates.add(text.substring(0, end + 1));
    for (final candidate in candidates) {
      final trimmed = candidate.replaceAll('\u0000', '').trim();
      if (!trimmed.startsWith('{')) continue;
      try {
        final data = jsonDecode(trimmed);
        if (data is Map<String, dynamic>) return data;
      } catch (_) {
        continue;
      }
    }
  } catch (_) {
    return null;
  }
  return null;
}

NcmSong parseNcm(String path) {
  final file = File(path);
  final raf = file.openSync();
  try {
    final size = raf.lengthSync();
    if (!_startsWith(_readExact(raf, 8), 'CTENFDAM')) {
      throw NcmException('不是有效的 NCM 文件（缺少 CTENFDAM 魔数）');
    }
    raf.setPositionSync(raf.positionSync() + 2);
    final keyLen = _u32(_readExact(raf, 4));
    if (keyLen <= 0 || keyLen > (1 << 20)) {
      throw NcmException('密钥块长度异常：$keyLen');
    }
    final keyBlob = _readExact(raf, keyLen);
    for (var i = 0; i < keyBlob.length; i++) {
      keyBlob[i] ^= 0x64;
    }
    var keyData = aesEcbDecryptUnpad(keyBlob, kCoreKey);
    if (!_startsWith(keyData, 'neteasecloudmusic')) {
      final aligned = keyBlob.length - (keyBlob.length % 16);
      if (aligned > 0) {
        keyData = Aes128(kCoreKey).decryptEcb(Uint8List.sublistView(keyBlob, 0, aligned));
      }
    }
    if (!_startsWith(keyData, 'neteasecloudmusic')) {
      throw NcmException('核心密钥解密结果异常，文件可能不是标准 NCM');
    }
    final keyBox = buildKeyBox(keyData.sublist(17));

    final metaLen = _u32(_readExact(raf, 4));
    var meta = <String, dynamic>{};
    if (metaLen > 0) {
      if (metaLen > (1 << 24)) throw NcmException('元数据块长度异常：$metaLen');
      meta = decryptMeta(_readExact(raf, metaLen)) ?? <String, dynamic>{};
    }

    _readExact(raf, 4); // CRC32
    _readExact(raf, 1); // 图片版本
    final coverFrameLen = _u32(_readExact(raf, 4));
    final imageSize = _u32(_readExact(raf, 4));
    var cover = Uint8List(0);
    if (imageSize > 0) {
      if (imageSize > (64 << 20)) throw NcmException('封面数据长度异常：$imageSize');
      cover = _readExact(raf, imageSize);
    }
    final extra = coverFrameLen - imageSize;
    if (extra > 0 && extra < (64 << 20)) {
      raf.setPositionSync(raf.positionSync() + extra);
    }
    final audioOffset = raf.positionSync();
    final audioSize = size - audioOffset;
    final head = _readExact(raf, 16);
    final rawMagic = xorWithKeystream(head, buildKeystream(keyBox));

    return NcmSong(
      path: path,
      size: size,
      audioOffset: audioOffset,
      audioSize: audioSize,
      keyBox: keyBox,
      cover: cover,
      meta: meta,
      rawMagic: rawMagic,
    );
  } finally {
    raf.closeSync();
  }
}

/// 把音频正文解密写到 dstPath，返回写入字节数。
int decryptAudioToFile(
  NcmSong song,
  String dstPath, {
  void Function(int done, int total)? onProgress,
  bool Function()? shouldStop,
}) {
  final src = File(song.path).openSync();
  final dst = File(dstPath).openSync(mode: FileMode.write);
  try {
    src.setPositionSync(song.audioOffset);
    final keystream = buildKeystream(song.keyBox);
    var done = 0;
    while (true) {
      if (shouldStop != null && shouldStop()) break;
      final chunk = src.readSync(kReadChunk);
      if (chunk.isEmpty) break;
      dst.writeFromSync(xorWithKeystream(chunk, keystream));
      done += chunk.length;
      onProgress?.call(done, song.audioSize);
    }
    return done;
  } finally {
    src.closeSync();
    dst.closeSync();
  }
}
