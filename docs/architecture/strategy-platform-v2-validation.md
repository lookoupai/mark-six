# 策略平台 V2 验证与上线操作

这里的测试只使用临时 SQLite、专用本机 MariaDB 和可删除 Docker volume。不要把测试环境变量指向运行中的数据库。现有服务、真实 GitHub 仓库和生产签名均未修改。

## 自动验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m coverage run --source=strategy_platform -m unittest discover -s tests -v
.venv/bin/python -m coverage report --fail-under=70
.venv/bin/python scripts/test_activation_issue.py
ENABLE_SCHEDULER=0 ENABLE_STARTUP_BACKTEST_WARMUP=0 ENABLE_STARTUP_ML_WARMUP=0 ENABLE_STARTUP_ZODIAC_BACKFILL=0 .venv/bin/python scripts/security_regression.py
npm ci
npm run browser:install
npm run test:e2e
```

浏览器测试启动独立临时应用，运行真实页面与 API；仅外部 AI 和开奖获取使用固定数据，所有非本机网络被阻止。覆盖创建、执行、订阅与取消、分享、编辑、历史、导出、预览导入、草稿发布、工作区切换、AI 确认、归档和移动宽度布局。`PLAYWRIGHT_CHROMIUM_EXECUTABLE` 可指定本机已安装 Chromium；`PYTHON` 可覆盖默认 `.venv/bin/python`。

移动端固定 Flutter **3.38.10**（Dart 3.10.9）、JDK 17、Android SDK 36，沿用 AGP 8.11.1 / Gradle 8.14。不要自动追最新 stable 导致工具链不兼容。

```bash
cd mobile/mark_six
flutter pub get
flutter analyze --no-fatal-infos --no-fatal-warnings
flutter test
flutter build apk --debug
```

既有 Flutter 警告与提示仍显示在 analyze 输出，CI 只将错误作为阻断。本轮未全面清理非策略页面的旧 lint。普通分支、PR 只验证和本地构建 debug APK，不上传 APK、不创建 Release；只有 `v*` tag 或显式运行 Android Release 工作流才签名发布。Docker 发布也依赖验证成功。

## MariaDB 升级与恢复演练

仅使用名为 `mark_six_validation`、用户 `validation`、主机 `127.0.0.1` 的一次性数据库；脚本会清空该专用库，不接受其它名称或远程主机。

```bash
export STRATEGY_TEST_DATABASE_URL='mysql+pymysql://validation:LOCAL_TEST_PASSWORD@127.0.0.1:LOCAL_TEST_PORT/mark_six_validation'
.venv/bin/python scripts/verification/strategy_mariadb_migration.py
.venv/bin/python -m unittest discover -s tests -p 'test_strategy_*.py' -v
unset STRATEGY_TEST_DATABASE_URL
```

编号迁移保存在 `strategy_schema_migrations`。版本 1 升级工作区匿名 ID、Outbox 事件类型与不可变内容快照，并兼容旧唯一约束；版本 2 仅将仍使用旧默认名称的默认工作区改为“策略工作区”，保留已自定义名称和稳定标识。SQLite DDL 在显式事务内执行；MariaDB 每个 DDL 步骤可恢复。账本只在全部成功后写入，不作为用户备份内容。系统导出/恢复测试包含策略、修订、订阅、运行、会话、提案和外键关系；清理过期回测时先解除提案引用。

## Docker 换容器持久化演练

先创建一次性 volume，不使用项目 `./data`。构建上下文通过 `.dockerignore` 排除了生产数据、密钥、本地依赖与 Git 历史。

```bash
docker build -t mark-six:validation .
docker volume create mark-six-validation-data
docker run --rm --network none --entrypoint python \
  -e STRATEGY_VALIDATION_ONLY=1 -e SECRET_KEY=local-validation-only \
  -e DATABASE_URL=sqlite:////app/data/validation.db -e ENABLE_SCHEDULER=0 \
  -e ENABLE_STARTUP_BACKTEST_WARMUP=0 -e ENABLE_STARTUP_ML_WARMUP=0 -e ENABLE_STARTUP_ZODIAC_BACKFILL=0 \
  -v mark-six-validation-data:/app/data mark-six:validation scripts/verification/strategy_persistence.py seed
docker run --rm --network none --entrypoint python \
  -e STRATEGY_VALIDATION_ONLY=1 -e SECRET_KEY=local-validation-only \
  -e DATABASE_URL=sqlite:////app/data/validation.db -e ENABLE_SCHEDULER=0 \
  -e ENABLE_STARTUP_BACKTEST_WARMUP=0 -e ENABLE_STARTUP_ML_WARMUP=0 -e ENABLE_STARTUP_ZODIAC_BACKFILL=0 \
  -v mark-six-validation-data:/app/data mark-six:validation scripts/verification/strategy_persistence.py check
docker volume rm mark-six-validation-data
```

两个不同容器分别写入/读取，检查帐号、owner、策略、修订、订阅、结果及其关联。完整启动还应使用默认 entrypoint/Gunicorn，确认 `/auth/login` 返回 200 且页面带 CSRF meta。

## 上线步骤与观测

- 先备份实际部署数据库，再在副本验证版本 1 升级；不要把系统全量备份上传到策略 GitHub 仓库。
- 设置随机 `SECRET_KEY`，保留 `./data:/app/data` 或已有 MySQL 数据库配置；新代码启动时执行编号迁移（当前最新为 2）。工作区只镜像已授权成员，重启不会授予成员权限。
- 工作区 owner 可导出独立策略包；本地恢复先校验摘要/Schema/引擎兼容，再确认导入私有草稿。草稿需再次确认发布，绝不自动订阅或执行。
- GitHub 是可选项。生产设置 `STRATEGY_GITHUB_TOKEN` 后，由 owner 配置指定私有仓库并触发同步，验证内容权限和首次真实提交；本轮仅验证模拟网络故障、限流、令牌失效、冲突和重试。
- 观察工作区备份页面的待同步/成功/失败计数；失败码不会包含令牌。归档同步 tombstone，已发布修订仍可追溯。单条 Outbox 提交后释放数据库写锁，再处理下一次远端请求。
- 上线后跨一个开奖周期核对旧订阅字段与 V2 行、自动运行和结算，再考虑结束兼容双写；当前仍保留旧字段作为回退。
- 多实例数据库锁/租约、生产真实 GitHub 凭据、签名 Release APK 和目标设备运行属于上线环境验收，本轮不能据隔离测试声称已完成。

## 工作区管理与策略编辑

- 后台侧栏 **策略工作区** 提供分组重命名和 **配置 GitHub 备份** 入口；用户管理的设置分组弹窗也能进入。备份按工作区共用，由该工作区所有者配置，站点部署统一提供凭据，普通成员不单独设置。站点管理员身份不会自动取得其他工作区的策略或备份权限。
- 工作区所有者也可在工作区顶部修改名称。名称更新不会改变稳定 ID、成员权限或策略。
- 复制策略时先填写名称；列表中的 **编辑策略** 可修改名称、支持地区和受限参数，保存为新版本。草稿编辑仍保持草稿，不发布、不排队备份。
- 生肖遗漏回补副本支持统计窗口 **10–240 期**、单双连开阈值 **2–20 期**，默认 **20 / 5** 与原算法一致，继续输出综合六肖和精选四肖；内置策略原设置不受副本修改影响。
- **AI 修改** 打开独立聊天页并恢复该策略最近的本人会话；当前策略、正文、历史与草案分别展示，可继续多轮讨论。错误以服务提示显示，失败消息可重试。AI 仍只生成草案，经回测和确认后发布。
- 本次 Python 路由、执行器和迁移变更需要重启应用后生效；迁移 2 自动处理旧默认名称。源码修改不会热更新当前无重载的业务进程。

新增专项用例：`python -m unittest discover -s tests -p test_strategy_workspace_management.py -v`。浏览器 `npm run test:e2e` 增加后台弹窗对比度、分组改名、备份入口、生肖副本命名/参数和已有策略 AI 修改焦点验证；同一暗色界面在操作系统深浅偏好下均验证标题/分组名对比度不低于 4.5:1。

## 独立策略聊天页验收（2026-11-25）

- 新页 `/user/strategy-assistant` 支持工作区与本人会话切换，消息分页加载，URL 定位会话；刷新后恢复正文、待确认草案和已有回测结果。
- 对话请求携带当前已发布规则与最新有效待确认草案。新草案替代旧待确认草案；发布新策略后，会话绑定该策略，后续讨论不会重复创建另一个策略。
- 供应商客户端限制、空/损坏返回与网络失败不会成为“成功回复”；错误以 `service` 历史展示。重试复用未得到有效回复的用户消息，避免重复历史。旧供应商拒绝文本保留原始数据库内容，仅展示时标为服务提示并从模型上下文排除。
- 本轮发现的真实服务反馈是“当前客户端不支持，仅限 Coding 客户端/CLI 使用”。此配置问题仍需管理员换成允许网页后端调用的 API；本轮不伪装客户端、不调用真实供应商进行付费验证，也不宣称真实 AI 已恢复。
- 新接口回归：`.venv/bin/python -m unittest discover -s tests -p test_strategy_assistant_chat.py -v`，10 项通过、0 失败、0 跳过（exit 0）。包括生肖副本经真实 API 运行/回测的映射修复。
- `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome npm run test:e2e`，exit 0；保留工作区原主路径并验证独立聊天正文、多轮讨论、刷新恢复、服务失败/重试、回测/发布。390px 视口等待侧栏退出和宽度稳定后，输入框可见且宽度不少于 310px。
- 此改动未增加数据库迁移版本；新页面/路由及服务响应处理需要重启当前应用后生效。运行中的业务服务与真实数据均未修改。
