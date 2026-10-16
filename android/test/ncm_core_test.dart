// 核心逻辑单元测试（不依赖 Flutter，可在 flutter test / dart test 下运行）。
//
// 关键的一项是 AES-128 的 FIPS-197 官方测试向量：解密 69c4e0d8… 应当得到
// 00112233445566778899aabbccddeeff。这条不对，整个 NCM 解密就全错。

import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:ncm2mp3/ncm_convert.dart';
import 'package:ncm2mp3/ncm_core.dart';
import 'package:ncm2mp3/ncm_tags.dart';

Uint8List hex(String text) {
  final out = Uint8List(text.length ~/ 2);
  for (var i = 0; i < out.length; i++) {
    out[i] = int.parse(text.substring(i * 2, i * 2 + 2), radix: 16);
  }
  return out;
}

void main() {
  group('AES-128', () {
    test('FIPS-197 官方测试向量（解密方向）', () {
      final key = hex('000102030405060708090a0b0c0d0e0f');
      final cipher = hex('69c4e0d86a7b0430d8cdb78070b4c55a');
      final plain = Aes128(key).decryptEcb(cipher);
      expect(plain, equals(hex('00112233445566778899aabbccddeeff')));
    });

    test('去 PKCS#7 填充', () {
      expect(pkcs7Unpad(Uint8List.fromList([1, 2, 3, 4, 4, 4, 4])),
          equals(Uint8List.fromList([1, 2, 3])));
      // 填充字节不合法时原样返回
      expect(pkcs7Unpad(Uint8List.fromList([1, 2, 3, 9, 9, 4, 4])),
          equals(Uint8List.fromList([1, 2, 3, 9, 9, 4, 4])));
    });
  });

  group('密钥盒与密钥流', () {
    test('生成的密钥盒是 0-255 的一个排列', () {
      final key = Uint8List.fromList(List.generate(16, (i) => i * 7 % 256));
      final box = buildKeyBox(key);
      expect(box.length, 256);
      final sorted = box.toList()..sort();
      expect(sorted.first, 0);
      expect(sorted.last, 255);
      expect(sorted.toSet().length, 256);
    });

    test('密钥流是 256 周期且可重复', () {
      final key = Uint8List.fromList(List.generate(20, (i) => (i * 13 + 5) % 256));
      final box = buildKeyBox(key);
      final ks1 = buildKeystream(box);
      final ks2 = buildKeystream(box);
      expect(ks1, equals(ks2));
      expect(ks1.length, 256);
    });

    test('异或两次还原（对称性）', () {
      final box = buildKeyBox(Uint8List.fromList(List.generate(16, (i) => i + 1)));
      final ks = buildKeystream(box);
      final data = Uint8List.fromList(List.generate(600, (i) => (i * 31) % 256));
      final once = xorWithKeystream(data, ks);
      expect(once, isNot(equals(data)));
      // 注意：分块调用时起点必须对齐 256，这里整体调用即可还原
      final twice = xorWithKeystream(once, ks);
      expect(twice, equals(data));
    });
  });

  group('输出路径推算', () {
    SourceItem item({String path = '/m/a.ncm', String rel = '', String artist = ''}) => SourceItem(
          path: path,
          relDir: rel,
          fmt: 'flac',
          artist: artist,
          title: '歌名',
        );

    test('默认输出到源目录', () {
      final options = ConvertOptions();
      final path = expectedOutputPath(item(), options).replaceAll('\\', '/');
      expect(path, '/m/a.flac');
    });

    test('指定输出目录并保留子目录结构', () {
      final options = ConvertOptions(outputDir: '/out', keepStructure: true);
      final path = expectedOutputPath(item(path: '/src/五月天/a.ncm', rel: '五月天'), options);
      expect(path.replaceAll('\\', '/'), '/out/五月天/a.flac');
    });

    test('直接放在源目录的文件按歌手归档', () {
      final options = ConvertOptions(outputDir: '/out', artistSubfolder: true);
      final path = expectedOutputPath(item(path: '/src/a.ncm', artist: '赵雷'), options);
      expect(path.replaceAll('\\', '/'), '/out/赵雷/a.flac');
    });

    test('用歌手-歌名命名', () {
      final options = ConvertOptions(outputDir: '/out', useSongName: true);
      final path = expectedOutputPath(item(artist: '赵雷'), options);
      expect(path.contains('赵雷 - 歌名'), isTrue);
    });
  });

  group('工具函数', () {
    test('文件名清理', () {
      expect(sanitizeFileName('a/b:c*d?'), 'a_b_c_d_');
      expect(sanitizeFileName('   '), 'untitled');
      expect(sanitizeFileName('正常名字'), '正常名字');
    });

    test('体积与时长显示', () {
      expect(humanSize(0), '-');
      expect(humanSize(1024), '1.0 KB');
      expect(humanSize(1536 * 1024), '1.5 MB');
      expect(humanDuration(0), '-');
      expect(humanDuration(65000), '1:05');
      expect(humanDuration(3725000), '62:05');
    });

    test('格式名显示', () {
      expect(describeFormat('mp3'), 'MP3');
      expect(describeFormat('flac'), 'FLAC');
      expect(describeFormat(''), '未知');
    });
  });
}
