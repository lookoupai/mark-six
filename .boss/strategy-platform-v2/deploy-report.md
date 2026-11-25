# 策略平台 V2 构建与部署准备

## 摘要

- 复用原 Docker/Compose 部署方式；只构建和验证一次性本地环境，没有替换运行中的服务。
- 镜像构建、Python 3.13 测试、两次容器读写同一临时 volume、默认 entrypoint/Gunicorn 登录页 200 已通过。
- `.dockerignore` 排除生产数据、凭据、Git、缓存与工具依赖；镜像内实际确认不存在这些文件。
- Flutter 固定 3.38.10；普通分支 CI 只验证，不上传 APK。签名 Release 仍需原有生产签名 Secrets。
- 真实 GitHub 凭据和生产数据库升级属于后续上线动作；本轮只演练专用测试数据库。

## 证据

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| Docker 构建 | exit 0 | `/tmp/mark-six-v2-docker-build-final.log` |
| 禁网容器写入 | PASS | `/tmp/mark-six-v2-docker-seed.log` |
| 换容器读取 | PASS | `/tmp/mark-six-v2-docker-recreate.log` |
| 正式验证脚本 | exit 0 / PASS | `/tmp/mark-six-final-persistence-script.log` |
| 镜像 Python 3.13 | 61 / 0 / 0，exit 0 | `/tmp/mark-six-final-python313-tests.log` |
| HTTP 健康 | `/auth/login` 200 且 CSRF meta 存在 | 主代理临时 Gunicorn 容器实测 |
| CI 发布边界 | actionlint exit 0；普通分支无 APK upload/release | `.github/workflows/validation.yml`、`android-release.yml` |
| Android debug APK | exit 0，构建成功；限制内存后再次通过 | `/tmp/mark-six-v2-pinned-apk-bounded.log`，Flutter 3.38.10 / JDK 17 |

完整重跑命令、环境变量和回退边界见 `docs/architecture/strategy-platform-v2-validation.md`。临时容器/volume 及本轮 Gradle/Kotlin daemon 已清理；测试 URL 不作为生产访问地址交付。

## 上线待办

1. 从实际部署数据库备份生成副本，执行编号迁移和恢复演练，再切换发行镜像。
2. 若启用 GitHub，设置服务器凭据、指定私有仓库，验证首次真实同步与队列状态。
3. 签名 APK 只经 tag/manual workflow 发布，并在目标手机安装验证。
4. 跨至少一个开奖周期对比兼容订阅字段和新运行/结算，保留回退字段。

**STATUS**: DONE_WITH_CONCERNS
**REASON**: 已完成可逆的本地构建/持久化/服务检查；生产部署与外部凭据验证未执行。

2026-11-25会话管理修正仍仅完成代码和隔离验证。上线时需重启业务进程，使目标保护、对话管理API及每60分钟的GitHub队列调度生效；正在运行的旧调度器不会因修改源码自动从1分钟切到60分钟。原先未绑定策略的历史对话由用户在页面显式选择关联，未对生产历史记录执行批量绑定或删除。
