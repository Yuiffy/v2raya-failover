# Configuration and deployment notes

The example profile is an editable starting point, not a target inventory. Keep the actual
profile and auth file outside the checkout with mode 0600. Installation normalizes runtime,
state, lock and backup paths to `/opt/v2raya-failover`, `/etc/v2raya-failover`,
`/var/lib/v2raya-failover` and `/run/v2raya-failover.lock`.

| Field | Meaning |
|---|---|
| `container`, `data_dir` | Existing Docker container and host directory containing `bolt.db` and `config.json`. No database writes are performed. |
| `api_url` | Numeric loopback HTTP URL on the same host, such as `http://127.0.0.1:2017`. Remote management endpoints are rejected. |
| `auth_file` | Private official login JSON (`username`, `password`) or an official `token`. Tokens expire; login credentials let each short run authenticate again. |
| `proxy_url` | Existing rule-aware HTTP proxy used for live health checks, commonly port 20172. |
| `verify_ports` | Host HTTP proxy ports rechecked after a switch, commonly 20171 and 20172. |
| `failure_threshold` | Consecutive failures before testing replacements; minimum two. A healthy run resets the counter. |
| `groups` | Existing v2rayA outbound names. Installation never creates outbound groups or silently selects nodes. |
| `subscription_indices` | Zero-based indices from `inspect`. Do not reuse an index from another server. |
| `name_prefixes` | Allowed beginnings of node display names. Candidates must also use VLESS TCP Reality. |
| `require_country` | Optional uppercase two-letter country code, checked using Cloudflare trace. Use only when required for the user's service. |
| `probes` | HTTPS URL, expected HTTP status, optional required response substring. All probes for a group must pass. |
| `routing_domains` | Optional domain suffixes routed to the group, including subdomains. Existing unrelated RoutingA rules remain after the marked block. |
| `subscription_outbound` | Optional managed outbound for subscription hosts. A separate download group avoids depending on the primary business node. |
| `refresh_subscription_indices` | Which subscriptions an explicitly requested `--refresh` may update. An empty list disables even manual updates through this tool. |
| `subscription_refresh_seconds` | `0` in the deployment profile. Installation rejects an automatic interval; change this deliberately only outside this deployment workflow if provider policy calls for it. |

The full managed block is shown in `install --dry-run`. It covers health-probe hosts and
configured business domains on `http`, `socks`, `rule-http` and `rule-socks` inbounds. A
subscription host is assigned to `subscription_outbound`. Native subscription fetch mode
is set to `pac`; native subscription auto-update is set to `none`. These changes are
included in the private installation manifest for removal.

Cloudflare country checks use the same host, so two groups cannot assign that host to
different outbounds. Choose a country constraint for the business group that needs it,
or adapt the probe design rather than disguising the conflict.

## Runtime and failure behavior

The watchdog checks configured service endpoints every minute after its preceding run
finishes. Two failed rounds trigger cached-candidate screening: TCP first, then independent
temporary Xray processes bound to loopback. It checks candidates in batches of six, does
not refresh a subscription as part of selection, rechecks for manual changes, backs up,
switches through the official API, and validates both HTTP ports. Validation failure
attempts to restore the previous selection. Each core reload can interrupt existing
connections; this is recovery, not connection-preserving load balancing.

The timer stops a run after 240 seconds and cleans up child Xray processes. Extremely
large or uniformly unreachable subscriptions may exceed that budget. Prefer a focused
set of cached candidates and inspect service logs if a run times out.

Manual `--refresh` preflights the fetched content before calling the native subscription
update. It rejects HTTP errors, HTML and fewer than five node links. The upstream is
fetched again by v2rayA, so preflight is not an atomic guarantee about a changing upstream
response; private backups remain the recovery source. It preserves a temporary-route
recovery journal before attempting an alternate route. The native subscription update
format and Bolt reader are version-specific, which is why a v2rayA upgrade needs validation.

## Troubleshooting and rollback

Use `journalctl -u v2raya-failover.service` and the private state file. Logs omit passwords,
tokens, subscription URLs, response bodies and node addresses. They contain outbound names,
subscription indices, node IDs and backup paths. Node names/addresses remain in private
v2rayA configuration and backups. Do not attach those backups to a public issue.

`manage.py uninstall` disables the timer, restores any journaled temporary subscription
override, removes its routing block, restores setting fields only when they still match
the values installed by this tool, and removes only its service/runtime files. It retains
private configuration, credentials, state and backups. If you later reinstall, archive
or explicitly move the retained `install-state.json` after inspecting it; the installer
refuses an existing installation manifest instead of overwriting recovery evidence.
