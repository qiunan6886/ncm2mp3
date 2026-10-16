// 本文件由 tool/make_fixture.py 自动生成，请勿手改。
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

const String kFixtureB64 = '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';
const String kExpectedAudioB64 = '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';
const String kExpectedCoverB64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

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
    expect(song.audioSize, 2058);
    expect(song.coverMime, 'image/png');
    expect(song.cover, equals(base64.decode(kExpectedCoverB64)));
    expect(song.musicName, '设备端测试曲目');
    expect(song.artistText, '测试歌手');
    expect(song.album, '测试专辑');
    expect(song.durationMs, 60000);

    // 2. 解密音频：必须与生成时的明文逐字节相同
    final output = File('${dir.path}/out.mp3');
    final written = decryptAudioToFile(song, output.path);
    expect(written, 2058);
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
    expect(output.lengthSync(), greaterThan(2058));

    // ignore: avoid_print
    print('设备端验证通过：产物 ${output.lengthSync()} 字节，音频 2058 字节');
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
