# 策略平台 V2 验证记录

## 摘要

- 后端 61 项、真实 MariaDB 专项 40 项、旧安全 68 项、激活码 6 项均通过；策略模块覆盖率 74%，门禁 70%。
- 浏览器完整主路径通过，Flutter 行为测试 4 项通过。
- 使用临时数据库与禁网/模拟外部服务；未读取生产数据库。
- 真实 GitHub 凭据、生产签名、实际手机安装运行与开奖周期观察未验证。

## 命令与结果

命令均从仓库根目录运行；Flutter 命令工作目录为 `mobile/mark_six`，PATH 指向固定 Flutter 3.38.10。

| 范围 | 实际命令 | 退出码 | 通过/失败/跳过 | 失败详情 |
| --- | --- | --- | --- | --- |
| 完整后端 | `.venv/bin/python -m coverage run --source=strategy_platform -m unittest discover -s tests -v` | 0 | 61 / 0 / 0 | 无 |
| 覆盖率 | `.venv/bin/python -m coverage report --fail-under=70` | 0 | 1 / 0 / 0 | 74% |
| MariaDB 专项 | `STRATEGY_TEST_DATABASE_URL=<专用本机验证库> .venv/bin/python -m unittest discover -s tests -p 'test_strategy_*.py' -v` | 0 | 40 / 0 / 0 | 无 |
| MariaDB 旧版本升级 | `STRATEGY_TEST_DATABASE_URL=<专用本机验证库> .venv/bin/python scripts/verification/strategy_mariadb_migration.py` | 0 | 1 / 0 / 0 | 无；含中断 DDL 恢复 |
| 既有安全回归 | `ENABLE_SCHEDULER=0 ENABLE_STARTUP_BACKTEST_WARMUP=0 ENABLE_STARTUP_ML_WARMUP=0 ENABLE_STARTUP_ZODIAC_BACKFILL=0 .venv/bin/python scripts/security_regression.py` | 0 | 68 / 0 / 0 | 无 |
| 激活码回归 | `.venv/bin/python scripts/test_activation_issue.py` | 0 | 6 / 0 / 0 | 无 |
| 网页端到端 | `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm run test:e2e` | 0 | 1 / 0 / 0 | 无；覆盖 14 个流程检查 |
| Flutter 行为 | `/tmp/mark-six-flutter-3.38.10/bin/flutter test` | 0 | 4 / 0 / 0 | 无 |
| Flutter 静态分析 | `/tmp/mark-six-flutter-3.38.10/bin/flutter analyze --no-pub --no-fatal-infos --no-fatal-warnings` | 0 | 1 / 0 / 0 | 0 error、9 warning、35 info，原有 lint 不隐藏 |
| Android APK | `JAVA_HOME=/tmp/mark-six-validation-jdk/jdk-17.0.21.1+2 ANDROID_HOME=/tmp/mark-six-validation-android-sdk /tmp/mark-six-flutter-3.38.10/bin/flutter build apk --debug --no-pub` | 0 | 1 / 0 / 0 | 无；生成本地 debug APK，未发布；2G/1G/2workers 限制配置再构建 52.3 秒通过 |
| Docker Python 3.13 | `docker run --rm --network none --entrypoint python -e PYTHONPATH=/app -v /home/mark-six/tests:/app/tests:ro mark-six-v2-validation:local -m unittest discover -s tests -v` | 0 | 61 / 0 / 0 | 无 |
| 正式持久化脚本 | 容器内 `STRATEGY_VALIDATION_ONLY=1 python scripts/verification/strategy_persistence.py check` | 0 | 1 / 0 / 0 | 无 |
| 工作流语法 | `actionlint .github/workflows/*.yml` | 0 | 1 / 0 / 0 | 无 |
| 依赖审计 | `npm audit` | 0 | 1 / 0 / 0 | 0 vulnerabilities |
| 补丁空白检查 | `git diff --check` | 0 | 1 / 0 / 0 | 无 |

## Mock 边界与真实验证

- AI 提供方与开奖结果获取被 mock；策略校验、回测、发布、运行、订阅、结算和存储均执行真实代码。
- GitHub HTTP 被 mock，验证 401/429/503/断网、远端冲突、重复写校验、策略包敏感字段及数据库保存独立性。
- Browser 使用真实 HTTP、CSRF、DOM，覆盖创建→运行→订阅/取消→分享→改参数→历史→导出→本地预览恢复→发布→切工作区→AI 回测/确认→归档及 390px 宽度无横向溢出。
- Docker 镜像禁网运行两个独立容器，核对账号、工作区、owner、策略、版本、订阅、运行结果与引用；默认 entrypoint/Gunicorn `/auth/login` 200 且带 CSRF。

覆盖率 74% 的统计范围是 `strategy_platform` 包；没有把未执行的生产凭据验证或整个历史 `app.py` 算作已覆盖。尚未建立历史大文件的增量行覆盖率统计，不能声称所有变更行达到 80%。

**STATUS**: DONE_WITH_CONCERNS
**REASON**: 已执行的隔离验证通过；签名发布、真实 GitHub 与设备/开奖周期验收留待上线。


## 工作区操作修正复核（2026-11-25）

| 范围 | 命令 | 退出码 | 通过/失败/跳过 | 结果 |
| --- | --- | --- | --- | --- |
| 本轮专项 | `.venv/bin/python -m unittest discover -s tests -p test_strategy_workspace_management.py -v` | 0 | 8 / 0 / 0 | 命名迁移、重命名权限/冲突、草稿不发布、参数默认兼容与边界、聊天目标、后台备份权限 |
| 全套回归 | `.venv/bin/python -m unittest discover -s tests -v` | 0 | 69 / 0 / 0 | 主代理独立复核；策略包与生肖算法覆盖率 76% |
| 网页操作 | `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm run test:e2e` | 0 | 1 / 0 / 0 | 后台分组改名/备份入口，复制命名、名称和参数编辑、已有策略 AI 滚动/焦点/会话，原有完整路径 |
| 弹窗可读性 | 同一浏览器用例中的 computed color/祖先背景合成对比度 | 0 | 2 / 0 / 0 | 系统深浅偏好下保留产品暗色；标题 15.55、分组名 13.98，均高于 4.5 |

主代理另对实际后台分组弹窗与策略编辑弹窗截图进行了目视检查。未连接用户已登录浏览器，全部使用隔离 fixture；未重启业务服务或修改真实数据库，迁移 2 在下一次应用启动时生效。

## 独立策略聊天页追加验收（2026-11-25）

最新后端全套 **79 / 0 / 0，exit 0**；`strategy_platform` 覆盖率 **76%**，70% 门禁通过。主代理执行命令：

```bash
COVERAGE_FILE=/tmp/mark-six-chat-verified.coverage .venv/bin/python -m coverage run --source=strategy_platform -m unittest discover -s tests -v
COVERAGE_FILE=/tmp/mark-six-chat-verified.coverage .venv/bin/python -m coverage report --fail-under=70
node --check static/js/strategy-assistant.js
git diff --check
```

新增 `test_strategy_assistant_chat.py` 的 10 项用例通过（包含在 79 项中），覆盖私有会话列表/分页、最新草案多轮上下文、发布后绑定同策略、供应商受限与无效回复、失败重试去重、可恢复校验失败，以及生肖副本实际 API 运行和回测。

扩展浏览器脚本仍覆盖分组/编辑/备份旧流程，并通过独立聊天正文、多轮讨论、刷新恢复、错误重试、回测后确认发布；390px 页面完整占用视口，输入框可见。实测命令：

```bash
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm run test:e2e
```

exit 0，1 条完整流程通过 / 0 失败 / 0 跳过；日志 `/tmp/mark-six-chat-e2e-final.log`。真实页面截图仅作内部目视：`/tmp/mark-six-strategy-chat-desktop.png`、`/tmp/mark-six-strategy-chat-mobile.png`；主代理另核验 fresh mobile。

当前真实 AI 供应商此前返回客户端限制信息；模拟服务验证不代表真实接口已经恢复。需配置支持网页后端调用的兼容接口。现业务服务未重启；页面与路由变更需重启生效，真实数据库内容未改写。

## 会话目标与历史管理追加验收（2026-11-25）

主代理最终独立复核：完整后端 **89 / 0 / 0，exit 0**，`strategy_platform` 覆盖率 **77%**，70%门禁通过。日志 `/tmp/mark-six-context-verified-tests.log`，覆盖率数据 `/tmp/mark-six-context-verified.coverage`。

```bash
COVERAGE_FILE=/tmp/mark-six-context-verified.coverage .venv/bin/python -m coverage run --source=strategy_platform -m unittest discover -s tests -v
COVERAGE_FILE=/tmp/mark-six-context-verified.coverage .venv/bin/python -m coverage report --fail-under=70
```

| 范围 | 实际命令 | 退出码 | 通过/失败/跳过 | 结果 |
| --- | --- | --- | --- | --- |
| 会话/调度专项 | `.venv/bin/python -m unittest discover -s tests -p test_strategy_chat_management.py -v` | 0 | 10 / 0 / 0 | 目标匹配、生肖输出上下文、显式关联、删除权限与保留范围、AI/回测删除竞争、SQLite主键复用、搜索隔离、3600秒调度与空队列日志 |
| 网页完整流程 | `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm run test:e2e` | 0 | 1 / 0 / 0 | 同策略新对话/40期需求、搜索删除且保留已发布策略、未绑定历史关联、初始策略读取失败锁住输入及刷新恢复，原有完整流程和390px布局 |
| 前端语法/补丁 | `node --check static/js/strategy-assistant.js`、`node --check tests/strategy_workspace_e2e.cjs`、`git diff --check` | 0 | 3 / 0 / 0 | 无语法错误或补丁空白错误 |

SQLite主键复用回归先验证旧实现错误接受了3种被替换的记录（会话创建时间、消息创建时间、消息正文变化），加原记录指纹核验后全部拒绝并返回409，未产生旧回复或草案。红/绿证据分别为 `/tmp/mark-six-chat-id-reuse-red.log`、`/tmp/mark-six-chat-id-reuse-green.log`；浏览器证据为 `/tmp/mark-six-context-e2e.log`。

已目视桌面和手机截图：长对话标题在历史列表限两行，完整标题保留悬停提示；手机聊天输入区可见，无横向溢出。真实HTTP/CSRF/数据库行为均使用临时fixture执行，AI只使用模拟响应并检查真实发送的系统上下文；没有调用真实AI或GitHub接口、修改真实会话、重启业务进程。60分钟调度需业务重启后生效。
