"""Disposable app fixture for browser E2E; external AI/draw feeds are mocked."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import signal
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_test_support import NUMBER_SPEC, isolated_app
from test_zodiac_rebound import example_history

module = isolated_app()
from models import db, User, StrategyWorkspace, StrategyWorkspaceMembership, LotteryDraw
from werkzeug.serving import make_server

with module.app.app_context():
    user = User(username="browser-owner", email="browser@example.invalid", password_hash="fixture-only",
                is_active=True, is_admin=True, activation_expires_at=datetime.now() + timedelta(days=30))
    db.session.add(user)
    db.session.flush()
    spaces = []
    for slug in ("browser-first", "browser-second"):
        space = StrategyWorkspace(slug=slug, name=slug)
        db.session.add(space)
        db.session.flush()
        db.session.add(StrategyWorkspaceMembership(workspace_id=space.id, user_id=user.id, role="owner"))
        spaces.append(space.id)
    for draw in example_history():
        db.session.add(LotteryDraw(region="macau", draw_id=draw["id"], draw_date=draw["date"],
            normal_numbers=",".join(draw["no"]), special_number=draw["sno"], special_zodiac=draw["sno_zodiac"], raw_zodiac=draw["raw_zodiac"]))
    db.session.commit()
    cookie = module.app.session_interface.get_signing_serializer(module.app).dumps({
        "user_id": user.id, "username": user.username, "is_active": True,
        "_csrf_token": "browser-fixture-csrf", "is_admin": True})

provider_attempts = {}

def provider_response(*args, **kwargs):
    messages = kwargs['json']['messages']
    latest = next(item['content'] for item in reversed(messages) if item['role'] == 'user')
    count = provider_attempts.get(latest, 0)
    provider_attempts[latest] = count + 1
    response = Mock()
    if latest == '测试接口限制' and count == 0:
        content = '本站仅限主流 Coding 客户端/CLI 使用。当前客户端不支持。'
    elif latest == '测试接口限制':
        content = json.dumps({'reply':'连接恢复了，可以继续讨论策略。', 'proposal':None})
    elif '40期的热门筛选' in latest:
        system_prompt = messages[0]['content']
        assert '当前正在修改已有策略' in system_prompt
        assert '当前策略名：生肖研究二号' in system_prompt
        assert '综合六肖' in system_prompt and '精选四肖' in system_prompt
        assert 'zodiac_groups' in system_prompt and 'frequency/highest' in system_prompt
        content = json.dumps({'reply':'当前修改“生肖研究二号”：保留综合六肖和精选四肖，可以讨论改为40期生肖频次排名。热门筛选会改变原遗漏回补依据，原四肖平衡不会自动保留；目前仅讨论，尚未发布。', 'proposal':None}, ensure_ascii=False)
    elif latest == '之前忘了关联策略，先保留这段讨论':
        assert '当前正在修改已有策略' not in messages[0]['content']
        content = json.dumps({'reply':'这段讨论尚未关联策略，可以先选择要继续修改的已有策略。', 'proposal':None}, ensure_ascii=False)
    elif '短期版' in latest:
        content = json.dumps({'reply':'可以先把统计窗口改为 10 期。你说的预防重复，是排除近期出现的生肖，还是保留它们重复出现时的覆盖？这两种规则不同，我们先明确。', 'proposal':None}, ensure_ascii=False)
    elif '先讨论' in latest:
        content = json.dumps({'reply':'保留已经讨论的 10 期窗口。历史重复并不能证明下期一定会重复，当前仍保留原六肖四肖筛选；可以继续比较覆盖与排除的取舍。', 'proposal':None}, ensure_ascii=False)
    else:
        is_zodiac = 'zodiac_rebound' in messages[0]['content']
        spec = {'schema_version':1, 'kind':'builtin_config','strategy_key':'zodiac_rebound', 'regions':['hk','macau'],
                'parameters':{'window':10,'parity_streak_threshold':6}} if is_zodiac else {**NUMBER_SPEC, 'window':10}
        content = json.dumps({'reply':'已整理 10 期版草案，保留原筛选规则，尚未发布。你可以继续讨论或先回测。',
            'proposal':{'name':'E2E assistant', 'change_summary':'统计窗口改为 10 期，保留原筛选依据', 'spec':spec}}, ensure_ascii=False)
    response.json.return_value = {'choices':[{'message':{'content':content}}]}
    return response

def stop(*_):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
with patch("requests.sessions.Session.request", side_effect=AssertionError("external network forbidden")), \
     patch("strategy_platform.api.requests.post", side_effect=provider_response), \
     patch.object(module, "get_ai_config", return_value={"api_key": "test", "model": "fixture", "api_url": "https://example.invalid"}), \
     patch.object(module, "_get_prediction_data", return_value=(example_history(), 2026)), \
     patch.object(module, "_get_next_period", return_value="2026326"):
    server = make_server("127.0.0.1", 0, module.app, threaded=True)
    print("BROWSER_FIXTURE " + json.dumps({"port": server.server_port, "cookie": cookie,
                                        "workspaces": spaces, "user_id": user.id}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
