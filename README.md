# 彩研所 - AI 数据分析预测系统

一个基于 Flask 的香港、澳门六合彩数据分析与智能预测平台，包含用户系统、激活码机制、管理后台、历史开奖数据、策略预测、自动学习、通知推送和移动端 Flutter 应用。

## 许可协议

本项目采用 `PolyForm Noncommercial License 1.0.0`。

- 允许查看、复制、分发和修改代码
- 仅限个人学习、研究、测试和其他非商业用途
- 禁止将本项目或其修改版本用于商业用途

正式协议见 [LICENSE](./LICENSE)，中文说明见 [LICENSE.zh-CN.md](./LICENSE.zh-CN.md)。

## 主要功能

### 用户与权限

- 用户注册、登录、修改密码
- 激活码开通与有效期控制
- 激活码申请与管理员发放
- 可选 GitHub 登录、Turnstile 人机验证
- 管理员后台用户管理

### 数据分析

- 支持香港、澳门开奖记录
- 历史开奖数据查询与展示
- 号码、生肖、波色、单双等多维统计
- 用户维度预测记录和准确率统计
- 回测快照与策略表现排行

### 智能预测

- 多策略预测：热门、冷门、走势、综合、均衡、马尔科夫、机器学习、AI、生肖遗漏回补
- 预测记录自动保存
- 特码命中、平码/生肖命中统计
- 支持同一期多策略预测汇总
- 可选按用户差异化预测，关闭后同一期同策略使用统一号码

「按用户差异化预测」不等于个人策略参数：号码策略基于相同的数据与地区级参数，
用用户 ID、地区、期号和策略生成固定轮换顺序，在高排名候选内选号，再按用户保存
记录。同一用户同一期重复请求复用记录，不同用户仍可能选到相同号码。生肖遗漏回补
使用固定规则，不根据用户轮换六肖、四肖；AI 也不走这套号码轮换逻辑。

AI 智能预测位于首页策略按钮区，需先在后台配置 AI API 地址、密钥和模型，
然后手动点击调用；自动预测不调用 AI。配置已填写不等于接口已验证可用。

### 生肖遗漏回补（六肖 / 四肖）

首页选择「生肖遗漏回补」，移动端也可选择同名策略。策略标识为
`zodiac_rebound`，香港、澳门分别使用各自历史数据；至少需要 20 期完整开奖。
用户中心的自动预测设置可单独勾选，新策略不会自动加入已有用户的订阅。

这里的 20 期指历史开奖，不是注册后累计的开奖。香港日常同步向官方请求最近
60 期；本策略发现样本不足时，先读取本地更早历史（可跨农历年），再尝试补取
官方历史，只新增缺失记录。补取失败仍保留 20 期门槛，提示到后台「香港开奖数据导入」
导入所需年份后重试，不需要等待后续开奖。回测不会触发补取，也不会读取目标期之后的数据。

- **综合六肖**：先取特码连续遗漏最多的四肖，再从其余生肖中取近 20 期总频次最高的两肖，去重后共六肖。
- **频次与遗漏**：总频次计入六个正码和一个特码，同期重复生肖按号码个数计数；遗漏以最近出现该特码生肖之后的期数计算，使用全部传入历史。历史内从未出现时，显示「至少遗漏 N 期」。
- **单双参考**：按对应农历生肖年的号码表判断，不能把某生肖永久视为单或双。特码同一单双连续出现至少 5 期时，反方向仅用于高频候选同频次时的排序。
- **精选四肖**：枚举六肖内的四肖组合，依次比较：接近两单两双、保留遗漏前二、保留上期正码出现至少两次且遗漏至少 10 期的生肖、减少其余上期轮空生肖、提高总频次、提高遗漏合计。同分时使用固定顺序，结果可复现。无法两单两双时明确显示退化结果。
- **输出与保存**：六肖、四肖、完整覆盖号码、十二生肖统计和解释保存在 `prediction_metadata` 的 `zodiac_rebound` 字段内。原有六个参考号码和一个代表号码继续保留以兼容记录系统，不能用这些代表号码的命中率代替组合覆盖命中率。
- **推送内容**：站内、邮件及 Telegram 等渠道的预测汇总会读取生成时保存的六肖、四肖，独立于「显示平码」开关；其他号码策略仍按该开关显示平码和特码。旧通知是生成时的快照，更新代码后新生成的通知才包含组合。

这是把示例中的主观选肖转成固定规则，并非对某一期组合的硬编码。
测试使用符合示例统计条件的**构造数据**，验证可得「兔、狗、牛、马、鸡、蛇」
和「兔、狗、马、鸡」；真实开奖数据不同，输出也会不同。
遗漏、重复和连单本身不意味着后续开奖概率提高。

逐期回测（仅使用目标期之前的数据）：

```bash
python scripts/backtest_predictions.py --region macau --strategies zodiac_rebound --min-history 20
python -m unittest discover -s tests -v
```

回测的 `strategy_results.zodiac_rebound.zodiac_groups` 分别列出六肖和四肖的
命中次数、有效样本数、命中率、随机覆盖基线及差值；有数据错误的样本不计入组合
有效样本数。基线按该期生肖号码表计算（每肖并非都恰好四个号码），跨农历年时也
按目标期生肖及映射计算。原有排行榜仍按代表号码口径，不用于评价六肖／四肖策略。
本策略使用固定规则，不参加号码策略自动调参或机器学习集成。

### 自动学习与优化

- 马尔科夫策略会学习转移概率、二阶转移、阶段转移、属性转移、失败反馈等信号
- 机器学习策略会参考其他本地策略近期表现，动态调整特征画像、运行画像、融合比例和集成偏好
- 新增自适应学习强度：
  - `responsive`：近期表现弱或样本不足时，提高近期权重并降低学习门槛
  - `balanced`：默认均衡模式
  - `conservative`：近期表现稳定时收紧学习强度，降低短期波动影响
- 管理后台支持策略自动优化，可按历史模拟回测择优调整参数
- 自动优化支持轻度、均衡、积极三档，并可配置最小提升阈值

### 通知与推送

- 站内通知中心
- 邮件通知
- Webhook、Telegram Bot、PushPlus、Bark 推送
- 预测汇总和命中通知支持球形号码展示
- 球形号码包含数字、生肖、红/绿/蓝波属性，站内通知和邮件样式同步

### 管理后台

- 激活码管理
- 激活码申请处理
- 系统配置管理
- 策略学习面板
- 预测记录和数据统计
- 自动优化开关与参数配置

### 移动端支持

- 提供移动端 API
- Flutter 移动端位于 `mobile/mark_six`
- 支持移动端登录、注册、激活、预测、预测记录、统计和通知相关数据

## 技术栈

### 后端

- Flask
- SQLAlchemy
- SQLite / MySQL / MariaDB
- APScheduler
- Requests

### 前端

- HTML5 / CSS3
- JavaScript
- Chart.js
- Font Awesome

### 移动端

- Flutter
- Dio
- Shared Preferences

## 安装与运行

### 环境要求

- Python 3.7+
- pip
- 可选：MySQL 或 MariaDB

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动项目

```bash
python run.py
```

启动后访问：

```text
http://localhost:5000
```

## 数据库

默认使用 SQLite：

```text
data/lottery_system.db
```

也可以通过环境变量使用 MySQL/MariaDB：

```bash
set DB_TYPE=mysql
set DB_HOST=localhost
set DB_PORT=3306
set DB_NAME=mark_six
set DB_USER=root
set DB_PASSWORD=your_password
```

或直接配置：

```bash
set DATABASE_URL=mysql+pymysql://user:password@host:3306/mark_six?charset=utf8mb4
```

更新已有数据库结构：

```bash
python auto_update_db.py
```

## 默认账号

首次部署时，第一个注册用户会自动成为管理员。

首次登录后请立即修改密码，并在后台配置必要的 AI、SMTP 和系统信息。

## 系统配置

### AI 配置

- `ai_api_key`
- `ai_api_url`
- `ai_model`

### 邮件配置

- `smtp_server`
- `smtp_port`
- `smtp_username`
- `smtp_password`
- `smtp_use_tls` — 启用 TLS（STARTTLS），默认开启，常用端口 587
- `smtp_use_ssl` — 启用 SSL（直连加密，常用端口 465），默认「自动」：
  启用 TLS 时默认关闭 SSL，未启用 TLS 时默认启用 SSL；可选强制开启/关闭
- `notify_email_enabled` — 邮件通道总开关，**默认开启**（表示“系统有能力发信”，
  不代表所有用户都收信）

邮件通知是**用户级订阅**，注册后默认关闭，由每个用户在「用户中心 → 推送设置 → 邮件通知」
自行开启。两层开关是「与」的关系：

| 层级 | 配置 | 默认 | 作用 |
| --- | --- | --- | --- |
| 系统 | `notify_email_enabled` | 开 | 邮件通道是否可用，SMTP 未配置时实际也发不出 |
| 用户 | `user_notify_<id>_email_enabled` | 关 | 该用户是否订阅通知邮件 |

按邮件类型区分：

| 类型 | 内容 | 受开关控制 |
| --- | --- | --- |
| 通知邮件 | 预测生成、命中提醒、系统通知、激活申请提醒 | 是（系统开关 + 用户订阅都需开启） |
| 账户邮件 | 邮箱验证、更换邮箱确认、重置密码 | 否（关掉会导致用户无法验证和找回密码） |

### 激活申请通知

- `activation_request_notify_emails`

说明：

- 可填写一个或多个邮箱，多个邮箱使用英文逗号分隔
- 用户提交激活码申请后，系统会发送通知邮件：优先发往 `activation_request_notify_emails` 配置的地址（配置即视为订阅），未配置时只发给已在用户中心开启邮件的管理员；站内通知不受影响
- 留空时默认通知所有管理员账号邮箱

### 系统展示配置

- `system_name`
- `system_description`
- `site_name`
- `site_description`
- `seo_title`
- `seo_description`

### 业务开关

- `allow_registration`
- `require_email_verification`
- `enable_turnstile`
- `enable_github_login`
- `enable_personalized_predictions`
- `auto_optimize_enabled`
- `auto_optimize_level`
- `auto_optimize_min_gain`

### 广告配置（Google AdSense）

| 配置键 | 默认 | 说明 |
|---|---|---|
| `adsense_enabled` | `false` | 是否启用 AdSense 广告 |
| `adsense_client_id` | 空 | 发布商 ID，`ca-pub-` 开头；格式不合法时广告不会加载 |
| `adsense_adsense_txt` | 空 | ads.txt 记录，一行一条；通过 `https://站点域名/ads.txt` 访问，留空则该路径返回 404 |

说明：

- 开启后在前台页面（分析主页、AI 聊天室、登录/注册）的 `<head>` 加载 AdSense 自动广告脚本；管理后台和用户中心不投放。
- 广告脚本自动带上 CSP nonce，相关 Google 广告域名（pagead2.googlesyndication.com、googleads.g.doubleclick.net、tpc.googlesyndication.com、fundingchoicesmessages.google.com）仅在开启时加入 CSP 白名单。
- 新配置项会在启动时通过 `auto_update_db.py` 自动写入 `system_config` 表，无需手动迁移。

## 使用流程

### 普通用户

1. 注册账号
2. 登录系统
3. 输入激活码，或提交激活码申请
4. 查看历史开奖数据
5. 使用号码预测功能
6. 在个人中心查看预测记录、准确率和通知

### 管理员

1. 登录后台
2. 配置 AI、SMTP 和系统展示信息
3. 处理激活码申请
4. 生成或发放激活码
5. 查看用户、数据、预测记录和策略统计
6. 按需要开启策略自动优化

## 定时任务

系统默认启用 APScheduler：

- 每天 21:40 自动更新开奖记录
- 每天 22:05 执行策略自动优化任务

可通过环境变量关闭：

```bash
set ENABLE_SCHEDULER=0
```

## 目录结构

```text
├── app.py
├── models.py
├── auth.py
├── admin.py
├── user.py
├── api_mobile.py
├── notification_service.py
├── activation_code_routes.py
├── invite_routes.py
├── auto_update_db.py
├── create_db.py
├── run.py
├── requirements.txt
├── templates/
├── static/
├── mobile/
└── data/
```

## 注意事项

1. 首次运行会自动创建数据库；第一个注册用户会自动成为管理员。
2. 使用 AI 相关功能前，需要先配置有效的 AI 接口信息。
3. 使用邮件通知前，需要先配置可用的 SMTP 服务。
4. 生产环境请使用安全的 `SECRET_KEY`。
5. 新版预测汇总通知的球形号码样式只影响新生成的通知，旧通知不会自动转换。
6. 如果调整数据库结构后部署到已有环境，请执行 `python auto_update_db.py`。
7. 如需通过 Python 重置管理员密码，可使用：

```bash
python reset_admin.py --username 你的管理员用户名 --password 新密码
```

## 策略工作区与 GitHub 备份

首个有效站点管理员对应默认策略工作区的 owner，其他账号需显式授权加入。owner 可以按用户名加入已注册账号，并授予“策略作者”或“普通成员”；策略工作区角色不会提升账号的站点管理员权限。策略作者可创建规则策略、与策略助手对话、回测草案并在确认后发布。
后台侧栏“策略工作区”可以修改分组名称并进入 GitHub 备份设置。备份按工作区共用，由工作区所有者配置，站点部署统一提供连接凭据；普通成员无需单独设置。复制策略可自定义名称，列表“编辑策略”支持重命名和参数修改；生肖遗漏回补副本的统计窗口/单双连开阈值默认 20/5，保持原算法。

可选的 GitHub 备份使用私有仓库。先在服务器环境中设置 `STRATEGY_GITHUB_TOKEN`，再由工作区 owner 在策略工作区配置 `owner/repository`。令牌只从部署环境读取，不保存在数据库或策略文件中。已发布的策略修订通过后台队列写入 GitHub，每 60 分钟检查一次待处理任务；没有新增修订时不会重复上传，空队列不再输出完成日志。草案、对话、用户资料和预测数据库不会写入策略仓库。Docker 部署继续把数据库和运行文件放在 `./data` volume 中。

AI 策略助手使用独立聊天页（用户中心侧栏 **AI 策略助手**，或策略列表 **AI 修改**）。页面保留本人对话记录、当前规则与待确认草案，刷新后可继续讨论；新草案的规则会传入后续讨论上下文。服务调用失败会显示具体提示并允许重试，不会伪装成已回复，也不会自动发布策略。所配置的 AI 接口必须允许普通服务端 API 调用；只接受特定 Coding 客户端/CLI 的接口不能直接用于本站策略助手。

从 **AI 修改** 进入后，**为此策略新建对话** 会保留当前策略，**讨论新策略** 才会进入未关联的新策略讨论。策略加载失败时发送保持禁用，刷新页面成功加载后可继续。以前未关联的对话可在右侧 **关联已有策略** 中选择自己的策略并确认；原聊天记录保留，之前待确认的草案会作废，后续讨论依据当前已发布规则。已关联的对话不能切换到另一策略。

对话记录可按标题或策略名称搜索，并分页加载。每条记录旁的 **删除** 会在确认后删除本人该对话、草案及其独占回测，保留已发布策略、修订、运行记录和备份队列；删除期间迟到的 AI 回复不会重建对话。聊天正文默认按 `strategy_chat_retention_days=90` 清理，历史会话条目仍可通过上述操作手动删除。

工作区支持切换、分享与撤回、手动/自动订阅和取消、运行记录及归档。owner 可直接上传导出的策略 JSON 包，预览后导入为私有草稿，再确认发布，无需 GitHub。网页和 Flutter 共用授权与 CSRF 校验；重启迁移只镜像已授权成员，不自动把其他账号加入工作区。

迁移、隔离测试、Docker 持久化及上线验证步骤见 [策略平台 V2 验证说明](docs/architecture/strategy-platform-v2-validation.md)。移动端固定 Flutter 3.38.10；普通分支和 PR 仅做测试/构建，APK 仅由 tag 或手动发布工作流发布。

## 安全与代理配置

系统的登录限流默认只认 TCP 层对端 IP，**不会**采信客户端自带的
`X-Forwarded-For`，避免伪造请求头绕过登录/注册限流。部署在反向代理之后时，
按实际情况选择一种方式：

| 部署方式 | 需要设置的环境变量 | 说明 |
| --- | --- | --- |
| 直接暴露端口（无代理） | 都不设置 | 使用对端真实 IP，最安全 |
| Nginx / Caddy 反代 | `TRUST_PROXY_HEADERS=1` | 由 `ProxyFix` 解析 `X-Forwarded-For` |
| 仅 Cloudflare 回源 | `TRUST_CF_HEADERS=1` | 使用 `CF-Connecting-IP` |

`docker-compose.yml` 默认把端口映射到 `127.0.0.1`，只允许本机反代访问。
如果改成 `5000:5000`（绑定 `0.0.0.0`）而同时保留 `TRUST_PROXY_HEADERS=1`，
客户端可直连并伪造转发头绕过限流。

其他安全策略：

- 密码长度要求 8–128 位，注册、重置密码、后台改密、移动端注册口径一致。
- 同一账号 15 分钟内连续失败 5 次会被临时锁定；同一来源 IP 15 分钟内上限 30 次。
  密码正确时始终放行，账号计数只影响错误密码的提示文案，避免被用来锁死真实用户。
- 通知中心的富文本内容在落库前与渲染前各做一次白名单净化（`html_sanitizer.py`），
  只保留排版标签，剥离脚本、事件属性与危险 CSS。
- 所有邮件正文里的用户名、邮箱、备注等用户可控字段均做 HTML 转义；纯文本通知
  统一经 `text_to_html()` 转义后再拼进邮件模板，邮件头也会去掉换行防注入。

### 内容安全策略（CSP）

页面响应默认带 CSP，`script-src` 采用 nonce + `strict-dynamic`：

- nonce 在 **模板编译期** 由 Jinja 扩展写入（`content_security.py`），只覆盖模板里
  真实存在的 `<script>`。用户注入的 HTML 不走模板编译，因此拿不到 nonce，被
  `script-src` 直接拦下——这也是不能在响应期扫描正文补 nonce 的原因。
- `object-src 'none'`、`base-uri 'self'`、`form-action 'self'`、`frame-ancestors 'self'`
  以及受限的 `connect-src` / `img-src` 切断外带通道：即使有脚本执行，也无法加载
  远程 payload 或把数据送出去。
- 静态文件与 JSON 响应使用 `default-src 'none'`，防止被当作文档解析。
- 内联事件处理器（`onclick=` 等）默认保留 `'unsafe-inline'`，以兼容现有页面写法。

相关环境变量：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `CSP_ENABLED` | `1` | 设为 `0` 关闭 CSP |
| `CSP_REPORT_ONLY` | 空 | 设为 `1` 时只发 `Content-Security-Policy-Report-Only`，不拦截 |
| `CSP_INLINE_HANDLERS` | `1` | 设为 `0` 时把内联事件处理器折算成 sha256 白名单（`'unsafe-hashes'`），更严格，但要求浏览器支持 `script-src-attr` |

首次上线建议先用 `CSP_REPORT_ONLY=1` 观察一两天：若浏览器控制台没有报错，再切到
强制模式。

## 免责声明

本系统仅供学习、研究和数据分析参考使用。任何预测结果都不构成投资、投注或其他决策建议。请理性使用。
