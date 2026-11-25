/// The server supplies permissions and capabilities; the client only presents
/// the current workspace and region, replacing stale rows on every refresh.
class StrategyDirectory {
  StrategyDirectory(List<Map<String, dynamic>> builtins,
      List<Map<String, dynamic>> strategies, String region) {
    for (final item in builtins) {
      final key = item['key']?.toString() ?? '';
      final capabilities = item['capabilities'];
      final regions = item['regions'];
      if (key.isEmpty || item['enabled'] == false || capabilities is! Map ||
          capabilities['manual_run'] != true ||
          (regions is List && !regions.contains(region))) {
        continue;
      }
      labels[key] = item['label']?.toString() ?? key;
      if (capabilities['clonable'] == true) _cloneable.add(key);
    }
    for (final item in strategies) {
      final key = item['id']?.toString() ?? '';
      final permissions = item['permissions'];
      final revision = item['revision'];
      final spec = revision is Map ? revision['spec'] : null;
      final regions = spec is Map ? spec['regions'] : null;
      if (key.isEmpty || item['status'] != 'active' || permissions is! Map ||
          permissions['run'] != true ||
          (regions is List && !regions.contains(region))) {
        continue;
      }
      labels[key] = item['name']?.toString() ?? key;
      custom[key] = item;
    }
  }

  final Map<String, String> labels = {};
  final Map<String, Map<String, dynamic>> custom = {};
  final Set<String> _cloneable = {};

  bool canCloneBuiltin(String key) => _cloneable.contains(key);
  bool canEdit(String key) => (custom[key]?['permissions'] as Map?)?['edit'] == true;
  String resolveSelection(String previous) =>
      labels.containsKey(previous) ? previous : (labels.keys.firstOrNull ?? '');
}

Map<String, dynamic>? selectStrategyWorkspace(
    List<Map<String, dynamic>> workspaces, int? previous) {
  for (final space in workspaces) {
    if (space['id'] == previous) return space;
  }
  return workspaces.firstOrNull;
}

String strategyResultText(Map result) {
  if (result['kind'] == 'number_selection') {
    final selection = result['selection'] as Map? ?? {};
    final special = selection['special'] as Map? ?? {};
    return '候选号码：${(selection['normal'] as List? ?? []).join('、')}\n'
        '主选号码：${special['number'] ?? '—'}';
  }
  final groups = result['groups'] as List? ?? [];
  final lines = groups.whereType<Map>().map((group) =>
      '${group['label'] ?? '结果组合'}：${(group['items'] as List? ?? []).join('、')}').toList();
  if (result['kind'] == 'external_selection' && (result['source'] as Map?)?['published_at'] == null) {
    lines.add('来源发布时间未知，暂不计入公平回测。');
  }
  return lines.join('\n');
}

String strategyBacktestText(Map? backtest) {
  if (backtest == null) return '';
  final summary = backtest['summary'] as Map? ?? {};
  final groups = summary['groups'] as Map? ?? {};
  final entries = <String, dynamic>{
    if (summary['special'] != null) '主选号码': summary['special'],
    if (summary['normal_coverage'] != null) '候选覆盖': summary['normal_coverage'],
    for (final entry in groups.entries) '${entry.key}组合': entry.value,
  };
  return entries.entries.map((entry) {
    final value = entry.value as Map;
    return '${entry.key}：命中 ${value['hits']}/${value['total']} 期，'
        '命中率 ${value['hit_rate']}%，随机基线 ${value['random_baseline']}%';
  }).join('\n');
}

String strategySpecText(Map spec) {
  const metrics = {'frequency': '出现频次', 'omission': '遗漏期数', 'special_frequency': '特码频次'};
  final parameters = spec['parameters'] as Map?;
  final window = spec['window'] ?? parameters?['window'];
  final groups = spec['groups'] as List? ?? [];
  return [
    if (window != null) '参考最近 $window 期',
    if (spec['normal_count'] != null) '候选号码 ${spec['normal_count']} 个',
    if (spec['rank_metric'] != null) '${metrics[spec['rank_metric']]}优先${spec['rank_order'] == 'lowest' ? '较低' : '较高'}',
    for (final group in groups.whereType<Map>()) '${group['label'] ?? '组合'} ${group['size']} 肖',
    if (spec['strategy_key'] == 'zodiac_rebound') '使用原生肖遗漏回补固定规则',
    if (spec['kind'] == 'external_selection') '使用澳门外部采集结果',
  ].join(' · ');
}
