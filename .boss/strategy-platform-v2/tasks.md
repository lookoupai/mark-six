# 策略平台 V2 续作任务

## 摘要

- 来源：用户指定 `docs/architecture/strategy-platform-v2.md`；Tech Lead 现场代码审查结论。审查/计划文件落盘后沿用其模块划分，不重新扩展需求。
- 当前是既有实现的验收与补齐：核心 API/模型/工作台已经存在；权限、回测隔离、恢复、客户端构建及测试门禁尚未完成。
- 共 13 个任务，按职责顺序开发；核心路径为隔离测试 → 权限与执行修复 → API/客户端接线 → 综合验证。
- Blast Radius：高，涉及权限、迁移、运行/备份及多端超过 10 个文件。用户已授权继续完成剩余工作；可逆代码及隔离测试直接推进，禁止操作生产数据、真实通知或擅自部署。
- 复杂度：高；预计 65–100 次有实质产出的读写/验证调用。共享文件只由下表指定 owner 写入。

## 仓库事实与边界

| 项目 | 已观察事实 |
| --- | --- |
| 分支 | 当前 `feature/strategy-platform-v2`，HEAD `8104d19`；文档中的 master 为方案基线，非本轮分支变更 |
| 工作区 | 存在大量此前未提交修改及未跟踪 `strategy_platform/`、`docs/`、工作台模板；必须保留 |
| 后端 | Flask 3.1 / Flask-SQLAlchemy 3.1 / SQLAlchemy 2；`.venv` 可用；依赖由 `requirements.txt` 管理 |
| 测试 | `python -m unittest discover -s tests -v`；此前仅 `tests/test_zodiac_rebound.py`，临时 SQLite、禁用调度和外网；基线 20 通过 / 1 失败，见 baseline 证据 |
| CI | 只有 Docker、Android 工作流；无 V2 测试门禁；Android 普通分支 push 会发布 APK，必须修正 |
| 客户端工具 | node、Docker 可用；Flutter/Dart 暂未找到；Playwright 位于 `/tmp/telesubmit-design-check/node_modules/playwright-core`，Chromium 缓存存在 |
| 契约 | `strategy_platform/api.py` 的 `/api/v2/workspaces/<id>/...` 路由；`catalog.py`、`dsl.py`、`models.py` 为当前权威 |
| 角色/状态 | workspace owner / strategy_author / member，与站点 is_admin 分离；策略 draft / active；proposal 状态及有效期以模型/服务为准且终态不得被回测复活 |
| 迁移 | 当前 create_all + 自定义 schema sync；缺少正式版本账本。新迁移必须幂等、兼容 SQLite/MySQL，不自动授予成员身份 |
| 未验证 | 真实 GitHub 凭据、生产 MySQL/MariaDB 并发、真实 Docker 换容器及 Flutter release 构建；不能写作已通过 |

测试一律临时数据库，不读取 `data/lottery_system.db`，关闭 scheduler、startup warmup 及外发请求。生产数据库变更不属于本轮验证操作。

## 文件 ownership 与并行安全组

| lane / owner | 独占写集 | 操作 | 写集约束 |
| --- | --- | --- | --- |
| API [BE] | `strategy_platform/api.py`、`access.py`、`legacy.py`；`user.py` 仅策略工作台路由；`tests/test_strategy_api.py` | 修改/新增 | 所有 API 路由统一由此 owner 落盘；接收 backup helper 和前端契约 |
| Backup [BE] | `strategy_platform/backup.py`、新增 `strategy_platform/bundles.py`、`tests/test_strategy_backup.py` | 修改/新增 | 不直接写 api.py；导入导出函数签名先发 API owner |
| Execution [BE] | `app.py`、`strategy_platform/service.py`、`results.py`、`runners.py`、`dsl.py`、`catalog.py`、`sources.py`；`tests/test_strategy_execution.py` | 修改/新增 | app.py 是中央文件，只此 owner 写；迁移启动 hook 由迁移 owner 提供说明 |
| Migration [BE] | `models.py`、`auto_update_db.py`、新增 `strategy_platform/migrations.py`、`tests/test_strategy_migrations.py` | 修改/新增 | 模型/迁移改动先告知 API/Backup；不自行编辑 app.py |
| Frontend [FE] | `templates/user/strategy_workspace.html`、所需 `static/js/`、`mobile/mark_six/lib/`、`mobile/mark_six/test/`、新增 `tests/strategy_workspace_e2e.*` | 修改/新增 | 需要 user.py/路由改动发 API owner；不编辑工作流 |
| DevOps/QA [SHARED] | `.github/workflows/`、部署/恢复说明、测试基础设施及 `.boss/strategy-platform-v2/` 验收记录 | 修改/新增 | 不与各 lane 测试文件重叠；需要 requirements 变更先协调单一 owner |

用户最新约束：最多 2 个并发（主任务 + 一个执行代理），各职责顺序推进。后端统一由单一实施代理负责，之后再进行前端、DevOps、QA；禁止派生更多代理。共享隔离测试位于 tests/strategy_test_support.py。

## 任务与验收

| ID | owner | 文件（除 lane 配套测试外） | 任务 / 验收 | 依赖 / 复杂度 |
| --- | --- | --- | --- | --- |
| T01 | QA | 共享隔离测试基础设施、baseline 记录 | 固定现有 21 项基线；所有新测试拒绝真实 DB/网络；保留失败快照 | 无 / 中 |
| T02 | API | `legacy.py`、`access.py` | 迁移只同步已授权 active membership，不能创建/复活成员或伪造 owner 授权审计；停用 workspace 拒绝直接 API 访问 | T01 / 高 |
| T03 | Migration | `models.py`、`migrations.py`、`auto_update_db.py` | 版本化、可重跑 schema 升级与历史 fixture 恢复；支持所需 owner 并发约束，确保恰一 active owner；保留旧记录 | T01 / 高 |
| T04 | API | `api.py` | owner 转移事务保护；他人查看/导出可审计；归档/停用、共享撤回、订阅撤销都按权限执行，保留历史结果 | T02 + T03 契约 / 高 |
| T05 | API | `api.py` | proposal 到期不可运行/发布，发布终态不可回测复活或重复创建；修改发布要求版本匹配和明确确认；owner 代发不能偷换作者 | T02 / 高 |
| T06 | Backup | `backup.py`、`bundles.py` | 429/5xx/断网仅待重试；远端已存在必须核验内容后判 synced；策略包校验摘要/Schema/兼容版本，排除用户资料、密钥、聊天 | T01 / 高 |
| T07 | API | `api.py` | 脱离 GitHub 的包预览/导入 API，统一调用 bundles；导入只生成 draft，不自动发布/订阅；预览差异可检查 | T06 helper 契约 / 中 |
| T08 | Execution | `app.py`、`service.py`、`dsl.py`、`runners.py` | builtin_config/ML 回测传递严格 cutoff，上下文不读未来数据；生肖 clone 使用原执行器；手动/自动/回测规则一致 | T01 / 高 |
| T09 | Execution | `results.py`、`app.py`、`sources.py` | 统一结果与结算口径；目标期生肖 fallback；正确处理来源时区/发布证明；修复坏旧记录借用同一期不相关 V2 结果的基线失败 | T08 / 高 |
| T10 | Frontend | `mobile/mark_six/lib/main.dart`、`api_client.dart` | 修复 `_backtest` 字段/方法重名和 nullable receiver；工作区选择、仅可运行策略、撤权后清理选择、取消订阅 | T04/T07 契约；编译修复可先做 / 中 |
| T11 | Frontend + API | FE 工作台模板；API owner 的 `user.py` 路由 | 工作区选择；按角色显示 AI/分享/克隆/归档/订阅撤销；包预览导入可完成草稿确认；主用户路径浏览器验证 | T04/T05/T07 / 高 |
| T12 | DevOps | `.github/workflows/android-release.yml`、验证工作流、部署恢复说明 | 普通分支/PR 只验证，不签名发布；tag/显式 dispatch 才发布；Python、Flutter lint/test/build 门禁按环境执行 | T10 / 中 |
| T13 | QA + Tech Lead | 验收记录、架构状态/剩余上线步骤 | 全套回归和 UI 主路径；临时 DB 导出→恢复核对；审查失败项闭环；分清已实现、已验证、部署环境待验 | T02–T12 / 高 |

## Contract Matrix

| ID | 用户承诺 / UI | Client Payload | Server / Persistence | 业务规则 | Test Evidence |
| --- | --- | --- | --- | --- | --- |
| C1 | 工作区仅受邀成员可见 | workspace_id + 当前登录身份 | membership、workspace.status | 重启迁移不能授权；停用/跨租户拒绝 | `test_strategy_api.py` / migration suite |
| C2 | AI 草案需确认，过期/已发布不可再次提交 | proposal_id、expected_revision、confirm | proposal terminal state、active_revision | 不覆盖活跃版本、不复活终态 | `test_strategy_api.py` |
| C3 | 导入预览后保存草稿 | bundle、预览摘要 | bundles validation → definition/revision draft | 摘要或兼容错误拒绝；无副作用发布 | `test_strategy_backup.py` + API tests |
| C4 | GitHub 故障显示待同步 | destination / outbox | pending、retry_at、verified content | DB 发布成功不被远端失败撤销 | `test_strategy_backup.py` |
| C5 | 手动、自动、回测同一规则和口径 | revision、region、target_period | version/cutoff/source snapshot/result | 不读目标期后数据；生肖组独立计分 | `test_strategy_execution.py` |
| C6 | 移动端/网页可切换工作区和撤销订阅 | workspace_id、strategy public_id | shared API access policy | 仅可见且 active 策略可运行；撤权刷新移除 | Flutter tests + browser E2E |
| C7 | 普通分支只验证，无 APK 发布 | Git event / ref | CI job guards / permissions | tag 或显式 dispatch 才签名发布 | workflow 静态检查 + CI |

## Evidence Waves 与停止条件

所有红测先写并观察真实失败，再修复；已有测试不得删断言或跳过以换绿。各 lane 配套测试文件名可由实现 owner微调，但变更必须同步此契约及 QA。

| Wave | 范围 / owner | 红测与绿门禁（argv） | Stop condition |
| --- | --- | --- | --- |
| W1 | 隔离、授权、迁移 / API+Migration | `[".venv/bin/python","-m","unittest","discover","-s","tests","-p","test_strategy_api.py","-v"]`；同命令替换 pattern 为 migrations | 任一测试连真实库/网络、迁移授予成员、owner 不唯一则阻止后续集成 |
| W2 | 执行与回测 / Execution | `[".venv/bin/python","-m","unittest","discover","-s","tests","-p","test_strategy_execution.py","-v"]`；原 zodiac suite | 存在未来数据、基线回归或口径不一致则不验收 |
| W3 | 备份与发布 / Backup+API | 同 unittest 命令，pattern `test_strategy_backup.py` / `test_strategy_api.py` | 导入自动执行、敏感信息外泄、429 永久失败或 replay 发布则不验收 |
| W4 | 客户端与 CI / Frontend+DevOps | `["flutter","analyze"]`、`["flutter","test"]`；浏览器 E2E 由前端给出实际 argv | 构建错误、主路径失败、普通分支发布制品则不验收；工具缺失如实记录 |
| W5 | 总验收 / QA | `[".venv/bin/python","-m","unittest","discover","-s","tests","-v"]` 加已实现 E2E 与恢复演练命令 | 任一未解决失败不得标记全完成；真实环境缺项独立列明 |

本轮已改为主任务 + 一个执行代理串行完成上述职责；实际验证命令由 CI 与验证指南保存，无并行写集。

## 状态

**STATUS**: DONE_WITH_CONCERNS
**REASON**: T01–T11 核心开发与主路径验证已完成；T12 已配置并通过工作流语法检查及固定 SDK 的 APK 构建；T13 隔离后端/真实数据库/浏览器/容器证据已记录，生产上线待办独立保留。


## 实施收尾（2026-11-25）

- T01–T09：完成；61 项完整 unittest、40 项 MariaDB 专项、68 项既有安全回归、6 项激活码回归通过；策略包覆盖率 74%。
- T10–T11：完成；Flutter 目录/草案行为测试 4 项通过，真实浏览器覆盖创建、执行、订阅/撤销、分享、编辑、恢复与发布等主路径及移动宽度。
- T12：已完成非发布 CI、tag/manual Release 边界、Docker 验证依赖、固定 Flutter 3.38.10；APK debug 构建 exit 0；限制内存后增量复核 52.3 秒通过。
- T13：已完成 SQLite/MariaDB 升级与外键恢复、Docker 换容器数据保留、Python 3.13 回归和 HTTP 健康检查；未部署生产或使用真实 GitHub 凭据。


## 工作区可用性修正（2026-11-25）

- 完成默认命名迁移 2（不覆盖自定义名称）、后台工作区管理和双入口重命名、按工作区 owner 的 GitHub 备份入口与权限说明。
- 复制可命名；现有策略和草稿可通过表单编辑名称/参数，草稿编辑不发布；生肖副本有限参数默认保持原算法。
- AI 修改立即显示目标策略、滚动/聚焦、反馈加载/失败并隔离切换会话的迟到响应。
- 8 项专项 API/算法/迁移回归通过；完整 unittest 69/69 通过，策略包与生肖算法覆盖率 76%；扩展浏览器完整流程通过，弹窗标题/分组名对比度为 15.55/13.98（均高于 4.5），手机编辑弹窗无横向溢出。
- 未重启运行中的服务；重启时应用新路由和迁移 2。

## 独立策略聊天页续作

- 完成独立路由、会话历史与刷新恢复、正文和当前规则/草案展示，工作区 AI 修改改为页面导航。
- 完成最新有效草案的后续对话上下文、发布新策略后会话绑定、供应商拒绝/无效响应提示和失败重试去重。
- 修复此次完整链路实际复现的生肖副本 API 映射错误，保持执行器统一按日期生成生肖映射。
- 验证：79 项后端测试通过，策略模块覆盖率 76%；独立聊天的多轮/恢复/重试/回测发布及移动布局 E2E 通过。
- 外部待办：管理员配置允许普通服务端调用的 AI 接口，重启当前业务服务使代码生效。本次未修改供应商凭据、真实数据或运行进程。

## 会话目标、历史管理与备份频率修正（2026-11-25）

- 已修复“为此策略新建对话”丢失当前策略的问题，新增独立“讨论新策略”；创建和发送带目标核验，加载失败时锁住发送并提示刷新。
- 已有策略上下文明确策略名、地区、六肖/四肖输出；40期热门方向需说明从遗漏回补改为生肖频次排名的算法差异，不再把已知输出当成从零提问。
- 本人对话支持标题/策略名搜索和确认删除；未绑定历史支持一次显式关联，保留消息并使旧草案作废。删除保留已发布策略/修订/运行/备份，仅清理所属聊天与独占回测。
- 删除与在途 AI 回复/回测并发已覆盖；SQLite 删除后复用会话及消息ID时，以原创建时间/消息内容再次核验，迟到回复不能写入新会话。
- GitHub 备份队列改为每60分钟检查；空队列不再输出完成日志，异常仍记录失败。当前业务进程未重启，旧进程的1分钟调度仍持续至重启。
- 10项新增专项测试通过；扩展E2E验证同策略新对话、40期上下文、搜索删除保留策略、旧对话关联、加载失败锁发送与刷新恢复，保留原有完整路径和390px移动布局。主代理最终独立全套89/89通过，策略模块覆盖率77%，70%门禁通过。
