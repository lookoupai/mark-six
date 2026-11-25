import 'package:flutter_test/flutter_test.dart';
import 'package:mark_six/strategy_directory.dart';

void main() {
  const builtin = [
    {'key': 'hot', 'label': '热门', 'enabled': true, 'regions': ['hk', 'macau'],
      'capabilities': {'manual_run': true, 'clonable': true}},
    {'key': 'ai', 'label': 'AI', 'enabled': false, 'regions': ['hk', 'macau'],
      'capabilities': {'manual_run': true}},
  ];
  test('only active permitted strategies for the selected region can run', () {
    final directory = StrategyDirectory(builtin, [
      {'id': 'draft', 'name': '草稿', 'status': 'draft', 'permissions': {'run': true}},
      {'id': 'revoked', 'name': '已撤权', 'status': 'active', 'permissions': {'run': false}},
      {'id': 'macau-only', 'name': '澳门', 'status': 'active', 'permissions': {'run': true},
        'revision': {'spec': {'regions': ['macau']}}},
      {'id': 'shared', 'name': '共享', 'status': 'active', 'permissions': {'run': true, 'edit': false},
        'revision': {'spec': {'regions': ['hk']}}},
    ], 'hk');
    expect(directory.labels.keys, ['hot', 'shared']);
    expect(directory.canEdit('shared'), isFalse);
    expect(directory.canCloneBuiltin('hot'), isTrue);
  });
  test('refreshing after revocation removes obsolete selections', () {
    final refreshed = StrategyDirectory(builtin, const [], 'hk');
    expect(refreshed.resolveSelection('revoked'), 'hot');
    expect(refreshed.custom, isEmpty);
  });
  test('workspace selection preserves valid selection and falls back after removal', () {
    const spaces = [{'id': 1, 'name': '一'}, {'id': 2, 'name': '二'}];
    expect(selectStrategyWorkspace(spaces, 2)?['id'], 2);
    expect(selectStrategyWorkspace(spaces, 99)?['id'], 1);
    expect(selectStrategyWorkspace(const [], 1), isNull);
  });
}
