# ncm2mp3（Android 版）

网易云 `.ncm` 转换器的安卓版本，和 Windows 版行为一致：

* **不重新编码**：mp3 出来还是 mp3，flac 出来还是 flac，音质一个字节都不动；
* **已转换的自动跳过**，反复运行只会补转新增的歌；
* 自动写入标题 / 歌手 / 专辑 / 封面，转换后自检（能发现「下载没下完」的残缺文件）。

## 安装

安装包在 `..\dist\ncm2mp3-android.apk`（构建产物在 `build\app\outputs\flutter-apk\app-release.apk`）。

传到手机上点开安装即可。因为是自签名、未上架应用商店的包，系统会提示「未知来源应用」，
允许安装即可；部分手机会提示风险，这是所有自行安装的 APK 都会有的提示。

## 首次使用

1. 打开应用 → 会提示需要 **「所有文件访问权限」**（Android 11 及以上）
   → 点「去授权」，在系统设置里找到 **ncm2mp3** 打开开关 → 回到应用点「我已授权，重新检查」。
   *（这个权限用来读取手机里的 .ncm、把结果写回音乐目录。没有它，安卓 11+ 无法访问共享存储。）*
2. **① 源目录**：点「选择」→ 进入内置的目录浏览器（有「内部存储 / 音乐 / 下载」快捷入口）
   → 选中放着 ncm 的目录 → 点「选择此文件夹」→ 自动开始扫描。
3. **勾选**：列表里每个文件前都有勾选框，默认全选；顶部有「全选 / 全不选 / 反选」。
   已经转换过的会标成绿色「已存在，将跳过」。
4. **② 输出目录**：默认输出到**源文件所在目录**；也可以点「选择」指定别处。
   下面一排开关：跳过已转换 / 保留子目录 / 按歌手归档 / 写标签封面 / 转换后校验。
5. **③ 开始转换**：底部按钮，带总进度与进度条，可随时「停止」。
   完成后弹窗显示成功/跳过/失败数量。右上角图标可展开日志。

设置会自动记住（存在应用私有目录的 `ncm2mp3_config.json`）。

## 与 Windows 版的对应关系

| 功能 | Windows | Android |
| --- | --- | --- |
| 解密核心 | `ncm2mp3/ncm.py`（Python） | `android/lib/ncm_core.dart`（Dart） |
| 标签写入 | `ncm2mp3/tags.py` | `android/lib/ncm_tags.dart` |
| 调度 / 跳过已转换 / 自检 | `ncm2mp3/core.py` | `android/lib/ncm_convert.dart` |
| 界面 | `gui.py`（tkinter） | `android/lib/main.dart`（Flutter） |

两边的判定逻辑一致：目标位置已有同名成品（含「同名不同后缀」）就跳过；
不重编码、保持原音质。

## 正确性验证

### 1. 桌面 Dart VM 上与官方实现逐字节比对

```powershell
# 在 android 目录下
dart run tool\verify.dart <输出目录> <ncm 文件>...
# 再用 Windows 版的比对工具，和官方 ncmdump.exe 逐字节比较音频正文
python ..\tools\verify_library.py ..\vendor\ncmdump\ncmdump.exe <ncm> <产物>
```

实测：**4/4 个文件（1 个 mp3 + 3 个 flac，含 130MB 无损）音频正文 sha256 与官方实现完全一致**；
中文标签读回正确；Windows 自带解码器能读出正确时长与标题。

### 2. 真实 Android 运行时上的端到端验证

`tool/make_fixture.py` 会生成一个**合成 NCM 样本**（真实的密钥块、元数据块、PNG 封面、
已知的音频正文），并把样本内嵌进 `integration_test/device_test.dart`。
这样在没有真机文件、没有存储权限的情况下，也能在设备上做「已知答案」断言：

```powershell
python tool\make_fixture.py fixture.ncm integration_test\device_test.dart
flutter test integration_test\device_test.dart -d <设备ID>
```

在 Android 17 模拟器（API 37）上实测结果：

```
解析结果：format=mp3 audio=2058 cover=70/image/png duration=60000
         name="设备端测试曲目" artist="测试歌手" album="测试专辑"
设备端验证通过：产物 2259 字节，音频 2058 字节
界面状态：权限引导页（未授权时正常显示）
00:08 +2: All tests passed!
```

也就是说设备端确认了：元数据解析、封面识别、**解密结果与生成时的明文逐字节相同**、
中文 ID3 标签写入后能正确读回、界面能正常启动。

### 3. 单元测试

`flutter test` 覆盖 AES-128 的 FIPS-197 官方测试向量、密钥盒 / 密钥流、
输出路径推算、文件名清理等，共 12 项全部通过。

### 顺带记两个真实 bug（都是这套比对抓出来的）

1. Dart 里 `Map<List<int>, …>` 用 List 当键是**按引用比较**的，导致 MP3 帧头表永远查不到，
   自检一直报「走帧失败」；
2. FLAC 的 Vorbis Comment 必须用 `utf8.encode`，用 `codeUnits`（UTF-16）会把中文截断成乱码。

（另外合成样本的生成器也踩过一次坑：元数据块的 22 字节前缀要和密文一起 XOR 0x63，
先 XOR 再跳前缀——这正是官方 `ncmcrypt.cpp` 的写法。）

## 自己构建

```powershell
cd android
flutter pub get      # 依赖极少（只用到 cupertino_icons）
flutter test
flutter build apk --release
```

产物：`android\build\app\outputs\flutter-apk\app-release.apk`

## 说明

* 应用**不联网**，所有解密都在本机完成，不会上传任何文件。
* 只申请 `MANAGE_EXTERNAL_STORAGE`（所有文件访问）与旧版读写权限，没有其他权限。
* 仅供把自己合法下载的音乐转成通用格式使用。
