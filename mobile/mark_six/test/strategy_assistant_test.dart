import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mark_six/api_client.dart';
import 'package:mark_six/main.dart';

void main() {
  testWidgets('assistant publishes only after backtest and explicit user confirmation', (tester) async {
    final requests = <RequestOptions>[];
    final dio = Dio();
    dio.interceptors.add(InterceptorsWrapper(onRequest: (options, handler) {
      requests.add(options);
      Map<String, dynamic> data;
      if (options.path.endsWith('/sessions')) {
        data = {'success': true, 'session_id': 7};
      } else if (options.path.endsWith('/messages')) {
        data = {'success': true, 'reply': '草案已生成', 'proposal_id': 9, 'expected_revision_id': null};
      } else if (options.path.endsWith('/backtests')) {
        data = {'success': true, 'backtest': {'sample_count': 10, 'summary': {
          'special': {'hits': 1, 'total': 10, 'hit_rate': 10, 'random_baseline': 2.04}}}};
      } else {
        data = {'success': true, 'item': {'name': '我的规则'}};
      }
      handler.resolve(Response(requestOptions: options, statusCode: 200, data: data));
    }));
    await tester.pumpWidget(MaterialApp(home: StrategyAssistantScreen(
        workspaceId: 1, region: 'macau', client: ApiClient.withTransport(dio))));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '按近20期出现频率筛选');
    await tester.tap(find.byIcon(Icons.send));
    await tester.pumpAndSettle();
    expect(find.text('确认发布'), findsNothing);
    expect(requests.any((request) => request.path.endsWith('/publish')), isFalse);
    await tester.tap(find.text('回测草案'));
    await tester.pumpAndSettle();
    expect(find.text('确认发布'), findsOneWidget);
    expect(requests.any((request) => request.path.endsWith('/publish')), isFalse);
    await tester.tap(find.text('确认发布'));
    await tester.pumpAndSettle();
    final publication = requests.singleWhere((request) => request.path.endsWith('/publish'));
    expect(publication.data['confirm'], isTrue);
    expect(find.text('策略“我的规则”已发布。'), findsOneWidget);
    expect(find.text('确认发布'), findsNothing);
  });
}
