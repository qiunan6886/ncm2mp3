"""生成一个合成的 NCM 样本（base64），供 Android 设备端测试做「已知答案」断言。

样本里包含：真实的密钥块（AES 加密 + PKCS#7 填充）、真实的元数据块、一张小 PNG 封面、
以及一段已知的音频正文。这样即使没有真机文件、没有存储权限，也能在设备上验证
「解析 → 解密 → 写标签」整条链路。

用法：
    python tool/make_fixture.py            # 打印 Dart 常量
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ncm2mp3.aes import AES                                       # noqa: E402
from ncm2mp3.ncm import CORE_KEY, build_key_box, build_keystream   # noqa: E402

META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")
RC4_KEY = b"0123456789abcdef"          # 16 字节，固定即可
AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 8
COVER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def pkcs7(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def encrypt(data: bytes, key: bytes) -> bytes:
    return AES(key).encrypt_ecb(pkcs7(data))


def build() -> bytes:
    parts = [b"CTENFDAM", b"\x01\x70"]

    # 密钥块：明文 = "neteasecloudmusic" + 16 字节密钥（PKCS#7 补到 48 字节）
    key_plain = b"neteasecloudmusic" + RC4_KEY
    key_blob = encrypt(key_plain, CORE_KEY)
    key_blob = bytes(b ^ 0x64 for b in key_blob)
    parts.append(struct.pack("<I", len(key_blob)))
    parts.append(key_blob)

    # 元数据块
    meta = {
        "musicId": 123456,
        "musicName": "设备端测试曲目",
        "artist": [["测试歌手", 1]],
        "album": "测试专辑",
        "bitrate": 320000,
        "duration": 60000,
        "format": "mp3",
    }
    meta_plain = b"music:" + json.dumps(meta, ensure_ascii=False).encode("utf-8")
    meta_cipher = encrypt(meta_plain, META_KEY)
    # 注意：官方实现是把「前缀 + base64 密文」整块 XOR 0x63，读的时候先整体 XOR 再跳过前 22 字节
    meta_blob = b"163 key(Don't modify):" + base64.b64encode(meta_cipher)
    meta_blob = bytes(b ^ 0x63 for b in meta_blob)
    parts.append(struct.pack("<I", len(meta_blob)))
    parts.append(meta_blob)

    # CRC32 + 图片版本 + 封面帧长度 + 封面长度 + 封面数据
    parts.append(b"\x00\x00\x00\x00")
    parts.append(b"\x01")
    parts.append(struct.pack("<I", len(COVER)))
    parts.append(struct.pack("<I", len(COVER)))
    parts.append(COVER)

    # 音频正文：明文按密钥流异或
    keystream = build_keystream(build_key_box(RC4_KEY))
    parts.append(bytes(b ^ keystream[i % 256] for i, b in enumerate(AUDIO)))
    return b"".join(parts)


DART_TEST = '''// 本文件由 tool/make_fixture.py 自动生成，请勿手改。
//
// 设备端验证：在真实 Android 运行时上跑「解析 → 解密 → 写标签」，
// 并用与桌面/官方实现一致的已知值做断言。样本内嵌，不依赖任何权限或外部文件。

import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:ncm2mp3/main.dart' as app;
import 'package:ncm2mp3/ncm_convert.dart';
import 'package:ncm2mp3/ncm_core.dart';
import 'package:ncm2mp3/ncm_tags.dart';

const String kFixtureB64 = '%(fixture)s';
const String kExpectedAudioB64 = '%(audio)s';
const String kExpectedCoverB64 = '%(cover)s';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('设备上跑通 解析 → 解密 → 写标签', (tester) async {
    final dir = Directory.systemTemp.createTempSync('ncm2mp3_test');
    final input = File('${dir.path}/fixture.ncm');
    input.writeAsBytesSync(base64.decode(kFixtureB64));

    // 1. 解析头部（元数据、封面、音频偏移）
    final song = parseNcm(input.path);
    // ignore: avoid_print
    print('解析结果：format=${song.audioFormat} audio=${song.audioSize} '
        'cover=${song.cover.length}/${song.coverMime} duration=${song.durationMs} '
        'name="${song.musicName}" artist="${song.artistText}" album="${song.album}"');
    expect(song.audioFormat, 'mp3');
    expect(song.audioSize, %(audioLen)d);
    expect(song.coverMime, 'image/png');
    expect(song.cover, equals(base64.decode(kExpectedCoverB64)));
    expect(song.musicName, '设备端测试曲目');
    expect(song.artistText, '测试歌手');
    expect(song.album, '测试专辑');
    expect(song.durationMs, 60000);

    // 2. 解密音频：必须与生成时的明文逐字节相同
    final output = File('${dir.path}/out.mp3');
    final written = decryptAudioToFile(song, output.path);
    expect(written, %(audioLen)d);
    expect(output.readAsBytesSync(), equals(base64.decode(kExpectedAudioB64)));

    // 3. 写标签并读回（中文）
    writeId3v23(
      output.path,
      {
        'title': song.musicName,
        'artist': song.artistText,
        'album': song.album,
        'encoder': kEncoderTag,
      },
      song.cover,
      song.coverMime,
    );
    final tags = readId3v23(output.path);
    expect(tags['TIT2'], '设备端测试曲目');
    expect(tags['TPE1'], '测试歌手');
    expect(output.lengthSync(), greaterThan(%(audioLen)d));

    // ignore: avoid_print
    print('设备端验证通过：产物 ${output.lengthSync()} 字节，音频 %(audioLen)d 字节');
    dir.deleteSync(recursive: true);
  });

  testWidgets('界面能正常启动', (tester) async {
    app.main();
    await tester.pump(const Duration(seconds: 5));
    final permissionView = find.text('去授权').evaluate().isNotEmpty;
    final mainView = find.text('扫描 ncm').evaluate().isNotEmpty;
    expect(permissionView || mainView, isTrue, reason: '应当渲染出权限引导页或主界面');
    // ignore: avoid_print
    print('界面状态：${permissionView ? "权限引导页（未授权时正常显示）" : "主界面"}');
  });
}
'''


def main() -> int:
    blob = build()
    args = sys.argv[1:]
    if args:                                   # 把原始样本写到指定路径
        with open(args[0], "wb") as handle:
            handle.write(blob)
        print(f"# 已写出样本：{args[0]}（{len(blob)} 字节）", file=sys.stderr)

    if len(args) > 1:                          # 生成内嵌样本的 Dart 测试文件
        content = DART_TEST % {
            "fixture": base64.b64encode(blob).decode(),
            "audio": base64.b64encode(AUDIO).decode(),
            "cover": base64.b64encode(COVER).decode(),
            "audioLen": len(AUDIO),
        }
        with open(args[1], "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        print(f"# 已生成测试：{args[1]}", file=sys.stderr)

    print(f"// 由 tool/make_fixture.py 生成，请勿手改；文件 {len(blob)} 字节，"
          f"音频 {len(AUDIO)} 字节，封面 {len(COVER)} 字节")
    print(f"const String kFixtureB64 = '{base64.b64encode(blob).decode()}';")
    print()
    print(f"const String kExpectedAudioB64 = '{base64.b64encode(AUDIO).decode()}';")
    print()
    print(f"const String kExpectedCoverB64 = '{base64.b64encode(COVER).decode()}';")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
