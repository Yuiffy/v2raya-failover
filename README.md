# v2rayA Failover

**English** | [简体中文](README.zh-CN.md)

[![Tests](https://github.com/Yuiffy/v2raya-failover/actions/workflows/test.yml/badge.svg)](https://github.com/Yuiffy/v2raya-failover/actions/workflows/test.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Cached-node failover for Linux Docker deployments of v2rayA and Xray, using real HTTPS health checks.
**Healthy nodes stay selected. Subscription updates are manual by default.** Optional Feishu switch alerts and a self-contained Codex deployment skill are included.

Use it with an existing v2rayA installation, subscription cache and RoutingA configuration to maintain separate business and download outbounds. It runs locally on the server through systemd; no chat session, model API or external scheduler is required.

## How it works

1. systemd runs the watchdog approximately 60 seconds after the preceding run finishes.
2. HTTPS probes use the actual proxy and check expected status codes and optional response text.
3. Two consecutive failed rounds trigger replacement screening within allowed cached subscriptions and node-name prefixes.
4. Candidates are screened by TCP and tested through temporary Xray processes bound to loopback. The active outbound is not cycled through candidates during testing.
5. The tool backs up configuration, switches through the official v2rayA API, validates both configured HTTP ports and attempts rollback if validation fails.
6. If enabled, a verified switch produces one Feishu notification attempt. Notification failure never rolls back the working proxy.

US business traffic and Hong Kong downloads are examples. Configure your own prefixes, probes and optional country constraint. Candidates currently support **VLESS + TCP + Reality** only.

| Example probe | Expected response | What it verifies |
|---|---|---|
| `https://api.openai.com/v1/models` | Expected unauthenticated 401 | Network/TLS/HTTP reachability, without a model call or API-key validation |
| `https://www.cloudflare.com/cdn-cgi/trace` | 200 and `loc=US` | Optional egress-country constraint |
| `https://pypi.org/simple/pip/` | 200 | Python download entry point |
| `https://registry-1.docker.io/v2/` | Expected Registry 401 | Docker Registry entry point |

A switch reloads the proxy core and may interrupt existing connections. This is recovery, not connection-preserving load balancing. If every cached node from a provider fails, another working path or provider is needed.

## Compatibility

- Linux, root, Python 3.10+, curl, Docker and systemd.
- The installer checks for **v2rayA 2.2.7.4**. The tested Xray version is **25.10.15**.
- Existing RoutingA, one working selected node per managed outbound and at least two supported cached candidates per group.
- Management and HTTP proxy ports must be reachable from the host. Bridge-network containers need correct port publishing and listening addresses.
- New v2rayA versions, other cores and native/non-Docker installations require separate validation.

This v2rayA version keeps only one selected node per outbound when using Xray, so this tool performs switching externally. Cache and management interfaces are version-specific; validate before upgrading.
See the upstream [connection implementation](https://github.com/v2rayA/v2rayA/blob/v2.2.7.4/service/server/service/connect.go), [subscription proxy selection](https://github.com/v2rayA/v2rayA/blob/v2.2.7.4/service/common/httpClient/httpClient.go) and [RoutingA API](https://github.com/v2rayA/v2rayA/blob/v2.2.7.4/service/server/controller/routingA.go).

## Deploy with the Codex skill

The [v2raya-failover skill](skills/v2raya-failover/SKILL.md) includes scripts and templates. An installed skill does not need a separate checkout.

Ask Codex:

> Use skill-installer to install skills/v2raya-failover from Yuiffy/v2raya-failover.

Reload skills, then ask:

> Use $v2raya-failover to inspect and deploy failover on my Linux v2rayA server, keeping subscription updates manual.

Alternatively, install the complete `skills/v2raya-failover` directory into your Codex skills directory. [Releases](https://github.com/Yuiffy/v2raya-failover/releases) provide a standalone `v2raya-failover-skill.zip` and SHA-256 checksums. Extract and install the enclosed `v2raya-failover` folder.
The skill inspects the target, current routing, subscription indices, allowed nodes and private authentication file, then previews changes, installs, verifies and prepares rollback.

## Manual deployment

Run these commands **on the target Linux server**. First make the required existing outbounds and RoutingA routes work. The example's `proxy`, `downloads` and subscription index `0` are placeholders for your environment. The installer does not create outbounds or select nodes.

```bash
git clone https://github.com/Yuiffy/v2raya-failover.git
cd v2raya-failover
sudo install -d -m 700 /etc/v2raya-failover
sudo install -m 600 skills/v2raya-failover/assets/config.example.json /etc/v2raya-failover/profile.json
sudo python3 skills/v2raya-failover/scripts/manage.py auth \
  --auth-file /etc/v2raya-failover/operator-auth.json \
  --api-url http://127.0.0.1:2017
```

The authentication command prompts locally and validates the official login. By default it stores plaintext credentials in a root-owned 0600 file; they are not encrypted or uploaded. Use `--token-only` to store an official token instead, but renew it when it expires. Management requests are restricted to a numeric loopback HTTP address.

Inspect indices/outbounds, edit the profile to match the actual container, mount, ports, nodes and routes, then preview the changes:

```bash
sudo python3 skills/v2raya-failover/scripts/manage.py inspect \
  --config /etc/v2raya-failover/profile.json
sudoedit /etc/v2raya-failover/profile.json
sudo python3 skills/v2raya-failover/scripts/manage.py install \
  --config /etc/v2raya-failover/profile.json --dry-run
```

After reviewing the plan, install and verify:

```bash
sudo python3 skills/v2raya-failover/scripts/manage.py install \
  --config /etc/v2raya-failover/profile.json
sudo systemctl status v2raya-failover.timer --no-pager
sudo journalctl -u v2raya-failover.service -n 20 --no-pager
sudo python3 /opt/v2raya-failover/v2raya_failover.py --check
```

Private backups are saved under `/var/lib/v2raya-failover/backups/`. Installation prepends a marked RoutingA block, preserves other rules and disables native subscription auto-update. An unhealthy initial route aborts installation and attempts to restore previous settings. See the [configuration and recovery reference](skills/v2raya-failover/references/configuration.md).

## Subscription update policy

**No scheduled or fault-triggered subscription updates by default.** Failover uses already cached nodes. Follow your provider's policy and your requirements. Deployment requires `subscription_refresh_seconds: 0`.

Only when you explicitly want to refresh:

```bash
sudo python3 /opt/v2raya-failover/v2raya_failover.py --refresh
```

Manual management-UI updates use the configured independent subscription outbound. Explicit script updates can also try direct access and an isolated primary node, restoring temporary routing afterward. Content is prechecked for errors and sufficient node links. The native updater fetches upstream again, so preflight does not guarantee identical responses; backups remain the recovery source.

## Feishu switch alerts (optional)

Disabled by default. Alerts are attempted only after a switch completes and both configured HTTP ports pass validation. Healthy checks, a first failed round and rolled-back switches do not send success messages.
Messages include the instance label, outbound, previous/current subscription indices and node IDs, server time and event ID. They omit node addresses, subscription URLs and credentials. Set `language` to `en` or `zh`.

Create a private bot file on the target:

```bash
sudo install -m 600 skills/v2raya-failover/assets/feishu.example.json /etc/v2raya-failover/operator-feishu.json
sudoedit /etc/v2raya-failover/operator-feishu.json
```

Replace `webhook` with the group's official HTTPS v2 custom-bot webhook. Set `secret` if signature verification is enabled; otherwise leave it empty. Configure keyword/IP restrictions to match your bot: messages contain the fixed keyword `v2rayA`. Never paste the webhook or secret into chat, logs or the repository. Signing follows the [official Feishu custom-bot protocol](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot).

Before installation, opt in through the private profile:

```json
"feishu": {
  "enabled": true,
  "config_file": "/etc/v2raya-failover/operator-feishu.json",
  "instance_name": "my-server",
  "language": "en",
  "timeout_seconds": 5
}
```

Installation copies the bot file to `/etc/v2raya-failover/feishu.json` with mode 0600. Dry-run and installation validation do not send test messages. Existing installations need the updated watchdog and its `feishu_notify.py` sibling before configuring `/etc/v2raya-failover/config.json`; editing configuration alone does not upgrade the code.

Delivery uses an independent direct HTTPS connection, ignores system proxy settings and rejects redirects. Both HTTP and business results are checked; HTTP 200 alone is not success. Failed delivery never rolls back a working proxy.
Each switch event has at most one send attempt. Timeouts or ambiguous outcomes are not retried automatically. Private receipts are stored at `/var/lib/v2raya-failover/notifications/<event-id>.json`; reconcile `pending` and `uncertain` results in the group before taking manual action. Delivery during a network outage is not guaranteed.

## Pause or uninstall

```bash
sudo systemctl disable --now v2raya-failover.timer
sudo systemctl stop v2raya-failover.service
sudo python3 skills/v2raya-failover/scripts/manage.py uninstall
```

Removal preserves selected nodes and private configuration, authentication, state and backups. It restores setting fields that still match this tool's applied values, and removes its service files and marked RoutingA block. Inspect and archive the retained installation manifest before reinstalling; it will not be overwritten. Never overwrite a running Bolt database.

## Development and verification

Runtime and tests use only the Python standard library:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q skills/v2raya-failover/scripts
```

Tests cover thresholds, node stability, candidate changes, rollback, official login, private-file permissions, configuration conflicts, read-only previews, install rollback, preservation of operator changes, manual-only subscriptions, notification signing/receipts/deduplication, secret protection and notification failure isolation.
Isolated fresh v2rayA containers validate installation, actual failover of two groups, absence of automatic subscription requests and removal. Notification tests use a mocked bot endpoint, not real groups. CI needs no subscription or server credentials.

Do not submit real configuration, databases, subscription URLs, passwords, tokens or runtime logs to the public repository.

## License

[MIT](LICENSE). This project does not bundle or redistribute v2rayA or Xray binaries.
