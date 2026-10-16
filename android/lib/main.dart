// ncm2mp3 —— 网易云 NCM 转换器（Android 版）
//
// 与 Windows 版行为一致：不重新编码，mp3 出来还是 mp3，flac 出来还是 flac；
// 已转换过的自动跳过；写入标题/歌手/专辑/封面；转换后自检。
//
// 文件访问方式：应用内置的目录浏览器 + dart:io（需要用户授予「所有文件访问权限」）。

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'ncm_convert.dart';

const MethodChannel _channel = MethodChannel('ncm2mp3/native');

void main() {
  runApp(const NcmApp());
}

class NcmApp extends StatelessWidget {
  const NcmApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ncm2mp3',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFF1A5FB4),
        useMaterial3: true,
      ),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  String _sourceDir = '';
  String _outputDir = '';
  List<SourceItem> _items = [];
  final Set<int> _selected = {};
  bool _scanning = false;
  bool _converting = false;
  bool _skipExisting = true;
  bool _keepStructure = true;
  bool _artistFolder = false;
  bool _writeTags = true;
  bool _verify = true;
  double _progress = 0;
  String _status = '就绪';
  final List<String> _logs = [];
  bool _permissionOk = false;
  bool _showLogs = false;
  bool _hadConfig = false;
  Isolate? _worker;
  ReceivePort? _workerPort;
  String _appDir = '';
  final Map<int, String> _rowStatus = {};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _bootstrap());
  }

  @override
  void dispose() {
    _worker?.kill(priority: Isolate.immediate);
    _workerPort?.close();
    super.dispose();
  }

  // ------------------------------------------------------------------ //
  // 初始化 / 权限 / 配置
  // ------------------------------------------------------------------ //

  Future<void> _bootstrap() async {
    try {
      _appDir = (await _channel.invokeMethod<String>('appDir')) ?? '';
      _permissionOk = (await _channel.invokeMethod<bool>('hasAllFilesAccess')) ?? false;
    } catch (_) {
      _permissionOk = true; // 非 Android 环境（桌面调试）
    }
    await _loadConfig();
    if (!mounted) return;
    setState(() {
      if (_sourceDir.isEmpty) {
        _sourceDir = Directory('/storage/emulated/0').existsSync()
            ? '/storage/emulated/0'
            : Directory.current.path;
      }
    });
    _log('存储访问：${_permissionOk ? '已授权' : '未授权'}');
    // 上次用过的目录还在就直接扫描，省一步操作
    if (_hadConfig && _permissionOk && _sourceDir.isNotEmpty && Directory(_sourceDir).existsSync()) {
      await _scan();
    }
  }

  Future<void> _requestPermission() async {
    try {
      await _channel.invokeMethod('requestAllFilesAccess');
    } catch (_) {}
    await Future<void>.delayed(const Duration(seconds: 1));
    var ok = false;
    try {
      ok = (await _channel.invokeMethod<bool>('hasAllFilesAccess')) ?? false;
    } catch (_) {
      ok = false;
    }
    setState(() => _permissionOk = ok);
    _log('申请存储访问：${ok ? '已授权' : '仍未授权'}');
  }

  String get _configPath =>
      _appDir.isEmpty ? '' : '$_appDir${Platform.pathSeparator}ncm2mp3_config.json';

  Future<void> _loadConfig() async {
    if (_configPath.isEmpty) return;
    try {
      final file = File(_configPath);
      if (!file.existsSync()) return;
      final data = jsonDecode(file.readAsStringSync()) as Map<String, dynamic>;
      setState(() {
        _sourceDir = (data['sourceDir'] ?? _sourceDir) as String;
        _outputDir = (data['outputDir'] ?? '') as String;
        _keepStructure = (data['keepStructure'] ?? true) as bool;
        _artistFolder = (data['artistFolder'] ?? false) as bool;
        _writeTags = (data['writeTags'] ?? true) as bool;
        _verify = (data['verify'] ?? true) as bool;
        _hadConfig = true;
      });
    } catch (_) {}
  }

  Future<void> _saveConfig() async {
    if (_configPath.isEmpty) return;
    try {
      File(_configPath).writeAsStringSync(jsonEncode({
        'sourceDir': _sourceDir,
        'outputDir': _outputDir,
        'keepStructure': _keepStructure,
        'artistFolder': _artistFolder,
        'writeTags': _writeTags,
        'verify': _verify,
      }));
    } catch (_) {}
  }

  void _log(String message) {
    if (!mounted) return;
    setState(() {
      _logs.add(message);
      if (_logs.length > 400) _logs.removeAt(0);
    });
  }

  // ------------------------------------------------------------------ //
  // 目录选择
  // ------------------------------------------------------------------ //

  Future<void> _pickSource() async {
    final path = await showDirectoryPicker(
        context, _sourceDir.isEmpty ? '/storage/emulated/0' : _sourceDir);
    if (path != null) {
      setState(() => _sourceDir = path);
      await _saveConfig();
      await _scan();
    }
  }

  Future<void> _pickOutput() async {
    final path = await showDirectoryPicker(
        context, _outputDir.isEmpty ? _sourceDir : _outputDir);
    if (path != null) {
      setState(() => _outputDir = path);
      await _saveConfig();
    }
  }

  // ------------------------------------------------------------------ //
  // 扫描
  // ------------------------------------------------------------------ //

  Future<void> _scan() async {
    if (_scanning || _converting) return;
    final dir = _sourceDir;
    if (dir.isEmpty || !Directory(dir).existsSync()) {
      _toast('请先选择一个存在的目录');
      return;
    }
    setState(() {
      _scanning = true;
      _status = '正在扫描…';
      _items = [];
      _selected.clear();
      _rowStatus.clear();
    });
    _log('扫描目录：$dir');
    try {
      final found = await Isolate.run(() => scan([dir]).map((e) => e.toJson()).toList());
      final items = found
          .map((e) => SourceItem.fromJson(Map<String, dynamic>.from(e as Map)))
          .toList();
      setState(() {
        _items = items;
        for (var i = 0; i < items.length; i++) {
          if (items[i].error.isEmpty) _selected.add(i);
        }
        _status = items.isEmpty ? '没有找到 ncm 文件' : '扫描完成：${items.length} 个文件（${_formatSummary(items)}）';
      });
      _log(_status);
      _refreshExisting();
    } catch (exc) {
      setState(() => _status = '扫描失败：$exc');
      _log('扫描失败：$exc');
    } finally {
      if (mounted) setState(() => _scanning = false);
    }
  }

  String _formatSummary(List<SourceItem> items) {
    final counter = <String, int>{};
    for (final item in items) {
      final key = item.fmtText;
      counter[key] = (counter[key] ?? 0) + 1;
    }
    return counter.entries.map((e) => '${e.key} ${e.value} 个').join('，');
  }

  ConvertOptions get _options => ConvertOptions(
        outputDir: _outputDir.isEmpty ? null : _outputDir,
        keepStructure: _keepStructure,
        artistSubfolder: _artistFolder,
        overwrite: !_skipExisting,
        writeTags: _writeTags,
        verify: _verify,
      );

  /// 标记哪些文件已经有转换结果了。
  void _refreshExisting() {
    final options = _options;
    var existing = 0;
    final next = <int, String>{};
    for (var i = 0; i < _items.length; i++) {
      if (_items[i].error.isNotEmpty) continue;
      final found =
          _skipExisting ? findExistingAudio(expectedOutputPath(_items[i], options)) : null;
      if (found != null) {
        existing++;
        next[i] = '已存在，将跳过';
      }
    }
    setState(() {
      _rowStatus
        ..clear()
        ..addAll(next);
    });
    if (existing > 0) _log('其中 $existing 个已有转换结果，将自动跳过');
  }

  // ------------------------------------------------------------------ //
  // 转换
  // ------------------------------------------------------------------ //

  Future<void> _startConvert() async {
    if (_converting || _scanning) return;
    final chosen = _selected.toList()..sort();
    if (chosen.isEmpty) {
      _toast('请先勾选要转换的文件');
      return;
    }
    if (_outputDir.isNotEmpty) {
      try {
        Directory(_outputDir).createSync(recursive: true);
      } catch (exc) {
        _toast('无法创建输出目录：$exc');
        return;
      }
    }
    await _saveConfig();

    setState(() {
      _converting = true;
      _progress = 0;
      _status = '转换中… 0/${chosen.length}';
      for (final i in chosen) {
        _rowStatus[i] = '排队中';
      }
    });
    _log('=' * 40);
    _log('开始转换 ${chosen.length} 个文件｜输出：${_outputDir.isEmpty ? '与源文件同目录' : _outputDir}');

    final port = ReceivePort();
    _workerPort = port;
    final items = [for (final i in chosen) _items[i].toJson()];
    final options = _options.toJson();
    port.listen((message) => _handleEvent(Map<String, dynamic>.from(message as Map), chosen));
    _worker = await Isolate.spawn(_convertEntry, [port.sendPort, options, items]);
  }

  void _handleEvent(Map<String, dynamic> event, List<int> chosen) {
    switch (event['t']) {
      case 'start':
        final index = chosen[event['i'] as int];
        setState(() => _rowStatus[index] = '转换中…');
        break;
      case 'progress':
        final fraction = (event['p'] as num).toDouble();
        final index = chosen[event['i'] as int];
        setState(() {
          _progress = fraction;
          _rowStatus[index] = '转换中 ${(fraction * 100).toStringAsFixed(0)}%';
        });
        break;
      case 'done':
        final index = chosen[event['i'] as int];
        final status = event['status'] as String;
        final message = (event['message'] as String?) ?? '';
        setState(() {
          _rowStatus[index] = switch (status) {
            'ok' => '完成',
            'skipped' => '已跳过',
            'canceled' => '已取消',
            _ => '失败：${message.length > 30 ? message.substring(0, 30) : message}',
          };
          _progress = 0;
        });
        break;
      case 'log':
        _log(event['m'] as String);
        break;
      case 'finished':
        final ok = event['ok'] as int;
        final skipped = event['skipped'] as int;
        final failed = event['failed'] as int;
        setState(() {
          _converting = false;
          _progress = 0;
          _status = '结束：成功 $ok｜跳过 $skipped｜失败 $failed';
        });
        _log('全部结束：成功 $ok，跳过 $skipped，失败 $failed');
        _workerPort?.close();
        _workerPort = null;
        _worker = null;
        if (mounted) {
          showDialog<void>(
            context: context,
            builder: (_) => AlertDialog(
              title: const Text('转换完成'),
              content: Text('成功 $ok 个\n跳过 $skipped 个\n失败 $failed 个'),
              actions: [
                TextButton(onPressed: () => Navigator.pop(context), child: const Text('好')),
              ],
            ),
          );
        }
        break;
    }
  }

  void _stopConvert() {
    _worker?.kill(priority: Isolate.immediate);
    _workerPort?.close();
    setState(() {
      _converting = false;
      _worker = null;
      _workerPort = null;
      _status = '已停止';
    });
    _log('已请求停止');
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  // ------------------------------------------------------------------ //
  // 界面
  // ------------------------------------------------------------------ //

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('ncm2mp3'),
        actions: [
          IconButton(
            tooltip: '日志',
            icon: Icon(_showLogs ? Icons.article : Icons.article_outlined),
            onPressed: () => setState(() => _showLogs = !_showLogs),
          ),
        ],
      ),
      body: !_permissionOk ? _permissionView() : _mainView(),
    );
  }

  Widget _permissionView() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(Icons.folder_off, size: 56, color: Colors.orange),
            const SizedBox(height: 16),
            const Text('需要「所有文件访问权限」',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 12),
            const Text(
              '这个权限用来读取手机里的 .ncm 文件、把转换结果写回音乐目录。\n'
              '在接下来的系统页面里找到本应用并打开开关即可。',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.grey),
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: _requestPermission,
              icon: const Icon(Icons.lock_open),
              label: const Text('去授权'),
            ),
            const SizedBox(height: 8),
            TextButton(
              onPressed: () async {
                var ok = false;
                try {
                  ok = (await _channel.invokeMethod<bool>('hasAllFilesAccess')) ?? false;
                } catch (_) {}
                setState(() => _permissionOk = ok);
              },
              child: const Text('我已授权，重新检查'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _mainView() {
    return Column(
      children: [
        _sourceCard(),
        _outputCard(),
        _listHeader(),
        Expanded(child: _fileList()),
        if (_showLogs) _logPanel(),
        _bottomBar(),
      ],
    );
  }

  Widget _sourceCard() {
    return Card(
      margin: const EdgeInsets.fromLTRB(8, 8, 8, 4),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.folder_open, size: 18),
                const SizedBox(width: 6),
                const Text('源目录', style: TextStyle(fontWeight: FontWeight.bold)),
                const Spacer(),
                TextButton(onPressed: _scanning ? null : _pickSource, child: const Text('选择')),
              ],
            ),
            Text(_sourceDir.isEmpty ? '（未选择）' : _sourceDir,
                style: const TextStyle(fontSize: 12, color: Colors.grey),
                maxLines: 2,
                overflow: TextOverflow.ellipsis),
            const SizedBox(height: 4),
            Row(
              children: [
                FilledButton.tonalIcon(
                  onPressed: _scanning || _converting ? null : _scan,
                  icon: _scanning
                      ? const SizedBox(
                          width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.search, size: 18),
                  label: Text(_scanning ? '扫描中…' : '扫描 ncm'),
                ),
                const Spacer(),
                Text('${_items.length} 个文件', style: const TextStyle(fontSize: 12)),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _outputCard() {
    return Card(
      margin: const EdgeInsets.fromLTRB(8, 4, 8, 4),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        child: Column(
          children: [
            Row(
              children: [
                const Icon(Icons.save_alt, size: 18),
                const SizedBox(width: 6),
                const Text('输出目录', style: TextStyle(fontWeight: FontWeight.bold)),
                const Spacer(),
                TextButton(onPressed: _converting ? null : _pickOutput, child: const Text('选择')),
              ],
            ),
            Align(
              alignment: Alignment.centerLeft,
              child: Text(_outputDir.isEmpty ? '默认：与源文件同一目录' : _outputDir,
                  style: const TextStyle(fontSize: 12, color: Colors.grey),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis),
            ),
            const Divider(height: 8),
            Wrap(
              spacing: 4,
              children: [
                _chip('跳过已转换', _skipExisting, (v) {
                  setState(() => _skipExisting = v);
                  _refreshExisting();
                }),
                _chip('保留子目录', _keepStructure, (v) => setState(() => _keepStructure = v)),
                _chip('按歌手归档', _artistFolder, (v) => setState(() => _artistFolder = v)),
                _chip('写标签封面', _writeTags, (v) => setState(() => _writeTags = v)),
                _chip('转换后校验', _verify, (v) => setState(() => _verify = v)),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _chip(String label, bool value, ValueChanged<bool> onChanged) {
    return FilterChip(
      label: Text(label, style: const TextStyle(fontSize: 12)),
      selected: value,
      onSelected: onChanged,
      visualDensity: VisualDensity.compact,
    );
  }

  Widget _listHeader() {
    final existing = _rowStatus.values.where((s) => s == '已存在，将跳过').length;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 2),
      child: Row(
        children: [
          Text('已选 ${_selected.length}/${_items.length}',
              style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold)),
          if (existing > 0)
            Padding(
              padding: const EdgeInsets.only(left: 8),
              child: Text('（$existing 个将跳过）',
                  style: const TextStyle(fontSize: 12, color: Colors.green)),
            ),
          const Spacer(),
          TextButton(
            onPressed: _items.isEmpty
                ? null
                : () => setState(() =>
                    _selected.addAll([for (var i = 0; i < _items.length; i++) i])),
            child: const Text('全选', style: TextStyle(fontSize: 12)),
          ),
          TextButton(
            onPressed: _items.isEmpty ? null : () => setState(_selected.clear),
            child: const Text('全不选', style: TextStyle(fontSize: 12)),
          ),
          TextButton(
            onPressed: _items.isEmpty
                ? null
                : () => setState(() {
                      final all = {for (var i = 0; i < _items.length; i++) i};
                      final inverted = all.difference(_selected);
                      _selected
                        ..clear()
                        ..addAll(inverted);
                    }),
            child: const Text('反选', style: TextStyle(fontSize: 12)),
          ),
        ],
      ),
    );
  }

  Widget _fileList() {
    if (_items.isEmpty) {
      return const Center(
        child: Text('还没有扫描结果\n点上面的「扫描 ncm」',
            textAlign: TextAlign.center, style: TextStyle(color: Colors.grey)),
      );
    }
    return ListView.builder(
      itemCount: _items.length,
      itemBuilder: (context, index) {
        final item = _items[index];
        final status = _rowStatus[index];
        return CheckboxListTile(
          dense: true,
          value: _selected.contains(index),
          onChanged: _converting
              ? null
              : (v) => setState(() {
                    if (v == true) {
                      _selected.add(index);
                    } else {
                      _selected.remove(index);
                    }
                  }),
          title: Text(item.name,
              maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 14)),
          subtitle: Text(
            '${item.fmtText} · ${item.durationText} · ${item.sizeText}'
            '${status == null ? '' : ' · $status'}',
            style: TextStyle(
              fontSize: 11,
              color: (status?.startsWith('失败') ?? false)
                  ? Colors.red
                  : status == '已存在，将跳过'
                      ? Colors.green
                      : Colors.grey,
            ),
          ),
          secondary: Icon(
            item.error.isNotEmpty ? Icons.error_outline : Icons.music_note,
            size: 20,
            color: item.error.isNotEmpty ? Colors.red : null,
          ),
        );
      },
    );
  }

  Widget _logPanel() {
    return Container(
      height: 140,
      color: Colors.black.withValues(alpha: 0.04),
      child: ListView.builder(
        padding: const EdgeInsets.all(8),
        itemCount: _logs.length,
        itemBuilder: (context, index) =>
            Text(_logs[index], style: const TextStyle(fontSize: 11, fontFamily: 'monospace')),
      ),
    );
  }

  Widget _bottomBar() {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (_converting) LinearProgressIndicator(value: _progress > 0 ? _progress : null),
            const SizedBox(height: 6),
            Row(
              children: [
                Expanded(
                  child: Text(_status,
                      style: const TextStyle(fontSize: 12),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis),
                ),
                const SizedBox(width: 8),
                if (_converting)
                  OutlinedButton.icon(
                    onPressed: _stopConvert,
                    icon: const Icon(Icons.stop, size: 18),
                    label: const Text('停止'),
                  )
                else
                  FilledButton.icon(
                    onPressed: _items.isEmpty || _scanning ? null : _startConvert,
                    icon: const Icon(Icons.play_arrow, size: 18),
                    label: const Text('开始转换'),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

// --------------------------------------------------------------------------- //
// 转换 isolate
// --------------------------------------------------------------------------- //

void _convertEntry(List<dynamic> args) {
  final SendPort port = args[0] as SendPort;
  final options = ConvertOptions.fromJson(Map<String, dynamic>.from(args[1] as Map));
  final items = (args[2] as List)
      .map((e) => SourceItem.fromJson(Map<String, dynamic>.from(e as Map)))
      .toList();

  final converter = Converter(options);
  var lastProgress = 0;

  final results = converter.run(
    items,
    onStart: (i, _) => port.send({'t': 'start', 'i': i}),
    onProgress: (i, p) {
      final now = DateTime.now().millisecondsSinceEpoch;
      if (now - lastProgress < 200 && p < 1) return;
      lastProgress = now;
      port.send({'t': 'progress', 'i': i, 'p': p});
    },
    onDone: (i, r) => port.send({
      't': 'done',
      'i': i,
      'status': r.status,
      'message': r.message,
    }),
    log: (m) => port.send({'t': 'log', 'm': m}),
  );

  port.send({
    't': 'finished',
    'ok': results.where((r) => r.status == 'ok').length,
    'skipped': results.where((r) => r.status == 'skipped').length,
    'failed': results.where((r) => r.status == 'failed').length,
  });
}

// --------------------------------------------------------------------------- //
// 目录浏览器（不依赖任何第三方插件）
// --------------------------------------------------------------------------- //

Future<String?> showDirectoryPicker(BuildContext context, String initial) {
  return showDialog<String>(
    context: context,
    builder: (_) => _DirectoryPicker(initial: initial),
  );
}

class _DirectoryPicker extends StatefulWidget {
  final String initial;
  const _DirectoryPicker({required this.initial});
  @override
  State<_DirectoryPicker> createState() => _DirectoryPickerState();
}

class _DirectoryPickerState extends State<_DirectoryPicker> {
  late String _current;
  List<String> _children = [];
  String? _error;

  @override
  void initState() {
    super.initState();
    _current = widget.initial.isEmpty ? '/storage/emulated/0' : widget.initial;
    _load();
  }

  void _load() {
    final dir = Directory(_current);
    try {
      final entries = dir.listSync(followLinks: false);
      final dirs = <String>[];
      for (final entry in entries) {
        if (entry is Directory) {
          final name = entry.path.split(Platform.pathSeparator).last;
          if (name.startsWith('.')) continue;
          dirs.add(entry.path);
        }
      }
      dirs.sort();
      setState(() {
        _children = dirs;
        _error = null;
      });
    } catch (exc) {
      setState(() {
        _children = [];
        _error = '$exc';
      });
    }
  }

  void _enter(String path) {
    setState(() => _current = path);
    _load();
  }

  Widget _shortcut(String label, String path) {
    if (!Directory(path).existsSync()) return const SizedBox.shrink();
    return ActionChip(
      label: Text(label, style: const TextStyle(fontSize: 12)),
      onPressed: () => _enter(path),
    );
  }

  @override
  Widget build(BuildContext context) {
    final parent = Directory(_current).parent.path;
    return AlertDialog(
      title: const Text('选择目录', style: TextStyle(fontSize: 16)),
      content: SizedBox(
        width: double.maxFinite,
        height: 420,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(_current, style: const TextStyle(fontSize: 11, color: Colors.grey), maxLines: 2),
            const SizedBox(height: 6),
            Wrap(spacing: 6, children: [
              _shortcut('内部存储', '/storage/emulated/0'),
              _shortcut('音乐', '/storage/emulated/0/Music'),
              _shortcut('下载', '/storage/emulated/0/Download'),
            ]),
            const Divider(),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.all(8),
                child: Text('无法读取该目录：$_error',
                    style: const TextStyle(color: Colors.red, fontSize: 12)),
              ),
            Expanded(
              child: ListView(
                children: [
                  if (parent != _current)
                    ListTile(
                      dense: true,
                      leading: const Icon(Icons.arrow_upward, size: 20),
                      title: const Text('返回上级', style: TextStyle(fontSize: 14)),
                      onTap: () => _enter(parent),
                    ),
                  for (final path in _children)
                    ListTile(
                      dense: true,
                      leading: const Icon(Icons.folder, size: 20, color: Colors.amber),
                      title: Text(path.split(Platform.pathSeparator).last,
                          style: const TextStyle(fontSize: 14),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis),
                      onTap: () => _enter(path),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('取消')),
        FilledButton(
          onPressed: () => Navigator.pop(context, _current),
          child: const Text('选择此文件夹'),
        ),
      ],
    );
  }
}
