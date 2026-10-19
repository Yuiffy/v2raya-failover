# v2rayA Failover

[![Tests](https://github.com/Yuiffy/v2raya-failover/actions/workflows/test.yml/badge.svg)](https://github.com/Yuiffy/v2raya-failover/actions/workflows/test.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

给 Linux Docker v2rayA + Xray 增加基于真实 HTTPS 请求的节点故障切换。
**健康时不换节点，默认不自动更新订阅。** 附带可独立安装的 Codex 部署 skill。

适合已有 v2rayA、节点订阅和 RoutingA 分流，希望把业务出口、下载出口分别维护的服务器。
这是服务器本地的定时脚本，不依赖聊天会话、模型 API 或外部调度平台。

## 工作方式

1. systemd 在每轮结束约 60 秒后执行下一次检查。
2. 通过实际代理访问配置中的 HTTPS 地址，检查状态码及可选响应片段。
3. 连续两轮失败后，从允许的订阅缓存和节点名称范围中筛选替代节点。
4. 候选先测 TCP，再用临时、只监听回环地址的 Xray 实测，避免拿生产出口轮流试错。
5. 切换前备份，通过 v2rayA 官方 API 切换，检查两个 HTTP 代理端口；失败尝试回退。

美国业务与香港下载只是示例，可按节点名称、探测地址和国家约束配置。
目前候选节点仅支持 **VLESS + TCP + Reality**。不宣称兼容其他节点传输协议。

| 示例检查 | 预期响应 | 验证范围 |
|---|---|---|
| `https://api.openai.com/v1/models` | 未认证请求返回 401 及预期错误信息 | 网络/TLS/HTTP 可达，不调用模型，不验证业务 API key |
| `https://www.cloudflare.com/cdn-cgi/trace` | 200，`loc=US` | 可选的出口国家约束 |
| `https://pypi.org/simple/pip/` | 200 | Python 下载入口可达 |
| `https://registry-1.docker.io/v2/` | Registry 的预期 401 | Docker Registry 入口可达 |

切换会重新加载代理核心，可能中断已有连接；它不是保持连接的负载均衡。
同一供应商所有缓存节点均失效时，需要其他健康路径或供应商，工具不能凭空恢复网络。

## 兼容性

- Linux、root、Python 3.10+、curl、Docker、systemd。
- 本版部署器校验 **v2rayA 2.2.7.4**；实际验证的 Xray 为 **25.10.15**。
- 已有 RoutingA；每个被管理的出站已经选中一个可用节点，并有至少两个受支持的缓存候选。
- 宿主机能访问管理端口和 HTTP 代理端口。Docker 桥接部署须正确配置端口映射和容器内监听地址。
- 新版 v2rayA、其他核心、原生 systemd 安装的 v2rayA 均需另行验证。

v2rayA 的该版本对 Xray 每个出站只保留一个连接节点，因此本工具在外围检测并切换。
实现依赖其版本相关的缓存格式和管理接口；升级 v2rayA 后应先验证。
依据官方 [连接实现](https://github.com/v2rayA/v2rayA/blob/v2.2.7.4/service/server/service/connect.go)、
[订阅代理选择](https://github.com/v2rayA/v2rayA/blob/v2.2.7.4/service/common/httpClient/httpClient.go)
及 [RoutingA 接口](https://github.com/v2rayA/v2rayA/blob/v2.2.7.4/service/server/controller/routingA.go)。

## 用 Codex skill 部署

仓库内的 [v2raya-failover skill](skills/v2raya-failover/SKILL.md) 自带脚本和模板，安装后无需另外克隆代码。

可以在 Codex 中说：

> 请用 skill-installer 从 Yuiffy/v2raya-failover 安装 skills/v2raya-failover。

安装后重新加载 skills，再说：

> 使用 $v2raya-failover，检查并部署我的 Linux v2rayA 服务器故障切换，保持订阅手动更新。

也可以将 `skills/v2raya-failover` 整个目录安装到你的 Codex skills 目录。
或者从 [Releases](https://github.com/Yuiffy/v2raya-failover/releases) 下载独立的
`v2raya-failover-skill.zip`，解压后安装其中的 `v2raya-failover` 文件夹；发布页附 SHA-256 校验文件。
skill 会先核对目标服务器、现有分流、订阅索引、节点范围和认证文件，再预览、部署、验证和准备回退。

## 手动部署

以下命令在**目标 Linux 服务器**执行。先调整 v2rayA 的出站和 RoutingA，使所需线路实际可用。
示例的 `proxy`、`downloads` 和订阅索引 `0` 不代表你的环境；安装器不会擅自创建出站或选节点。

```bash
git clone https://github.com/Yuiffy/v2raya-failover.git
cd v2raya-failover
sudo install -d -m 700 /etc/v2raya-failover
sudo install -m 600 skills/v2raya-failover/assets/config.example.json /etc/v2raya-failover/profile.json
```

在服务器本地输入 v2rayA 账号密码，验证官方登录并保存私有凭据：

```bash
sudo python3 skills/v2raya-failover/scripts/manage.py auth \
  --auth-file /etc/v2raya-failover/operator-auth.json \
  --api-url http://127.0.0.1:2017
```

默认将登录凭据以明文保存在 root 所有、0600 权限的文件中，不做加密，也不会上传到仓库。
如果不希望保存密码，可加 `--token-only` 保存官方登录 token，但到期后需续期。
管理接口仅允许数字回环 HTTP 地址，不通过远程网络传输凭据。

查看订阅索引及已有出站，然后编辑 profile，匹配实际容器、配置挂载、端口、节点名称及分流：

```bash
sudo python3 skills/v2raya-failover/scripts/manage.py inspect \
  --config /etc/v2raya-failover/profile.json
sudoedit /etc/v2raya-failover/profile.json
sudo python3 skills/v2raya-failover/scripts/manage.py install \
  --config /etc/v2raya-failover/profile.json --dry-run
```

确认计划中的出站、域名和设置正确后安装：

```bash
sudo python3 skills/v2raya-failover/scripts/manage.py install \
  --config /etc/v2raya-failover/profile.json
sudo systemctl status v2raya-failover.timer --no-pager
sudo journalctl -u v2raya-failover.service -n 20 --no-pager
sudo python3 /opt/v2raya-failover/v2raya_failover.py --check
```

安装器在 `/var/lib/v2raya-failover/backups/` 保存私有备份，在原 RoutingA 前加入带标记的
管理规则，保留其他规则，关闭 v2rayA 自带订阅自动更新。初始线路验证失败会中止并尝试恢复原设置。
查看完整的 [配置及恢复说明](skills/v2raya-failover/references/configuration.md)。

## 订阅更新政策

**默认不定期更新，也不因节点故障自动更新。** 切换使用已保存的节点缓存，两件事互不依赖。
是否以及何时更新，按订阅厂商说明和你的需要决定。部署器要求 `subscription_refresh_seconds: 0`。

只有你明确需要更新时，才执行：

```bash
sudo python3 /opt/v2raya-failover/v2raya_failover.py --refresh
```

管理界面的手动更新默认走配置的独立订阅出口。脚本的显式更新还能在该线路失败时尝试直连和
独立实测的主节点，并在结束后恢复临时分流。它先检查响应不是错误页面且有足够节点，再调用原生更新。
原生更新会再次请求上游，因此预检不是对两次响应完全相同的保证；备份仍是恢复依据。

## 暂停与卸载

```bash
sudo systemctl disable --now v2raya-failover.timer
sudo systemctl stop v2raya-failover.service
```

需要移除工具和管理规则时：

```bash
sudo python3 skills/v2raya-failover/scripts/manage.py uninstall
```

卸载保留当前选中的节点及私有配置、认证、状态和备份，恢复尚未被其他操作修改的设置字段，
移除自己的服务文件和 RoutingA 标记块。重新安装前先检查并归档保留的安装清单，安装器不会覆盖它。
不要直接覆盖运行中的 Bolt 数据库。

## 开发与验证

运行时和测试仅依赖 Python 标准库：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q skills/v2raya-failover/scripts
```

验证覆盖连续失败阈值、健康时不切换、候选变更、失败回退、官方登录、私有文件权限、
配置冲突、只读部署预览、安装失败回退、卸载保留用户修改，以及禁用自动订阅更新。
另外使用隔离的全新 v2rayA 容器验证部署、两组实际故障切换、无自动订阅请求和卸载恢复；
不在用户正在使用的出站上注入故障。CI 不需要订阅或服务器凭据。

不要将真实配置、数据库、订阅链接、密码、token 或运行日志提交到公开仓库。

## License

[MIT](LICENSE)。本项目不打包或分发 v2rayA/Xray 二进制文件。
