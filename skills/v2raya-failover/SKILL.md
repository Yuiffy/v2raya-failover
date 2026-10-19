---
name: v2raya-failover
description: Deploy, inspect, configure or remove cached-node failover for a Linux Docker v2rayA/Xray server, including independent outbound probes and manual subscription-update recovery. Use for automatic node switching and this tool's deployment; not for installing a VPN provider or purchasing subscriptions.
---

# v2rayA failover deployment

Use the bundled [manager](scripts/manage.py), [watchdog](scripts/v2raya_failover.py), and
[configuration reference](references/configuration.md). They are self-contained; an installed
skill does not need a separate checkout. Runtime dependencies are Linux, root, Python 3.10+,
curl, Docker and systemd. This release targets v2rayA 2.2.7.4; Xray 25.10.15 is the tested core.

## Establish the target and preserve existing choices

- Confirm the user's intended server, Docker container, management/proxy ports, configuration
  mount, existing outbound names, subscription indices and desired node regions. Inspect live
  state; do not infer these from the example profile.
- Preserve the existing routing outside the tool's managed block. Keep business services
  separate from download/subscription traffic if that is the user's existing design.
- Use the official login API through a loopback endpoint and an operator-provided private
  auth file. Do not derive/sign authentication tokens from cached configuration. Never ask
  the user to paste passwords, subscription URLs or bearer tokens into chat; have them
  provision the file on the target, or run the interactive `auth` command there.
- Default to cached-node failover only. Leave `subscription_refresh_seconds` at `0` and
  do not run `--refresh` without a user request to update subscriptions. A failed probe is
  not authorization to update a subscription. Honor the provider's update policy.

## Prepare a concrete deployment

1. Copy the scripts and assets to a private staging directory on the target. Keep their
   relative structure (`scripts/` beside `assets/`), and create a 0600 profile from
   [the example](assets/config.example.json). The example's regions and outbound names are examples.
2. Authenticate with an existing root-only credential file or:
   `python3 scripts/manage.py auth --auth-file /etc/v2raya-failover/operator-auth.json`.
   The default stores official login credentials in a 0600 file; `--token-only` stores
   an expiring official token instead and requires renewal. Do not reset an existing account.
3. Run `python3 scripts/manage.py inspect --config /private/path/profile.json` and adapt
   the profile to the actual outbound names, zero-based subscription indices, node-name
   prefixes, probe endpoints, expected HTTP responses, country constraints and business domains.
   Check that each group has at least two supported cached VLESS TCP Reality nodes.
4. Require existing RoutingA and a working selected node per group. If these prerequisites
   are absent, explain the specific gap and prepare the needed configuration within the user's
   requested scope. Do not switch unrelated services or change all traffic to one region.
5. Run `python3 scripts/manage.py install --config /private/path/profile.json --dry-run`.
   Inspect the actual routing and settings plan before installation. Dry-run does not change
   v2rayA, install files or enable a timer. Continue with already authorized deployment;
   ask only if the plan exceeds the user's authorization or requires a missing choice.

## Install and verify

Run `python3 scripts/manage.py install --config /private/path/profile.json` as root.
It saves private backups, preserves unrelated routing, adds marked rules, disables native
subscription auto-update, installs the watchdog and enables the systemd timer. An unhealthy
initial route aborts installation and attempts to restore the original routing/settings.

Verify `v2raya-failover.timer` is enabled/active and service exit status is zero. Inspect a
completed automatic timer run and confirm the subscription attempt/success timestamps did
not advance. Use `python3 /opt/v2raya-failover/v2raya_failover.py --check` for read-only health
checks. Do not inject a failure into the user's active service just to demonstrate failover;
use an isolated container if fault injection is warranted.

Explain that probes are HTTPS requests through the real proxy, not just ping. An expected
OpenAI 401 validates connectivity without an API key or model call; it does not validate
the user's paid API key or inference service. A switch reloads the proxy core and may
interrupt existing connections. All cached nodes failing is not something the tool can
fix without another healthy path or provider.

## Operate and remove

- Routine runs do not refresh subscriptions. Only when the user requests an update, run
  `python3 /opt/v2raya-failover/v2raya_failover.py --refresh`. It prevalidates the response
  and can try the configured route, direct access, and an isolated primary node. Temporary
  routing is restored and interrupted overrides are recovered on the next run.
- Pause with `systemctl disable --now v2raya-failover.timer`, then stop the service.
- Remove with `python3 scripts/manage.py uninstall`. It removes only the managed routing
  block and installed service/runtime files, preserves other settings changes, keeps the
  current selected nodes, and retains private config/auth/backups for recovery. Never
  overwrite the live Bolt database to roll back; stop the container first if an operator
  explicitly chooses a full database restore.
- Report what was installed/changed, actual probe results, subscription policy, backup
  location and rollback command. Do not publish target configuration, credentials or logs.
