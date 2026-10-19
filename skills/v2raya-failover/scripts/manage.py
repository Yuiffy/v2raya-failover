#!/usr/bin/env python3
"""Inspect, install and remove the local v2rayA failover service."""

import argparse
import copy
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

import v2raya_failover as watchdog

ASSETS = Path(__file__).resolve().parent.parent / "assets"
BEGIN = "# BEGIN v2raya-failover managed rules"
END = "# END v2raya-failover managed rules"


class ConfigurationError(ValueError):
    pass


def layout(root):
    root = Path(root).resolve()
    return {
        "runtime": root / "opt/v2raya-failover/v2raya_failover.py",
        "config": root / "etc/v2raya-failover/config.json",
        "auth": root / "etc/v2raya-failover/auth.json",
        "manifest": root / "var/lib/v2raya-failover/install-state.json",
        "state": root / "var/lib/v2raya-failover/state.json",
        "backup": root / "var/lib/v2raya-failover/backups",
        "lock": root / "run/v2raya-failover.lock",
        "service": root / "etc/systemd/system/v2raya-failover.service",
        "timer": root / "etc/systemd/system/v2raya-failover.timer",
    }


def strip_managed(routing):
    if BEGIN not in routing and END not in routing:
        return routing
    if routing.count(BEGIN) != 1 or routing.count(END) != 1:
        raise ConfigurationError("Ambiguous managed routing block; inspect it manually")
    start = routing.index(BEGIN)
    end = routing.index(END) + len(END)
    if end < start:
        raise ConfigurationError("Malformed managed routing block")
    return routing[:start] + routing[end:].lstrip("\r\n")


def host_of(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
        raise ConfigurationError("Probe/subscription URL has an unsupported host")
    return host


def validate(config, api):
    if config.get("subscription_refresh_seconds", 0) != 0:
        raise ConfigurationError("Deployment defaults to manual-only subscriptions; set interval to 0")
    if config.get("failure_threshold", 2) < 2:
        raise ConfigurationError("Use a failure threshold of at least two")
    if not config.get("groups"):
        raise ConfigurationError("At least one outbound group is required")
    outbounds = api.call("outbounds")["outbounds"]
    connected = api.call("touch")["touch"]["connectedServer"]
    for name, group in config["groups"].items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*", name) or name not in outbounds:
            raise ConfigurationError("A configured outbound does not exist or has an unsupported name")
        if len([x for x in connected if x["outbound"] == name]) != 1:
            raise ConfigurationError("Each managed outbound must already have one selected node")
        if not group.get("name_prefixes") or not group.get("subscription_indices") or not group.get("probes"):
            raise ConfigurationError("Each group needs name prefixes, subscription indices and probes")
        country = group.get("require_country")
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            raise ConfigurationError("require_country must be an uppercase two-letter code")
        matches = 0
        for index in group["subscription_indices"]:
            if not isinstance(index, int) or index < 0 or index >= len(api.subs):
                raise ConfigurationError("A subscription index is out of range (indices start at zero)")
            matches += sum(watchdog.supported(raw.get("serverObj", {})) and any(raw.get("serverObj", {}).get("ps", "").startswith(p) for p in group["name_prefixes"]) for raw in api.subs[index].get("servers", []))
        if matches < 2:
            raise ConfigurationError("Each managed group needs at least two supported cached candidates")
        for probe in group["probes"]:
            host_of(probe["url"])
            if urllib.parse.urlsplit(probe["url"]).scheme != "https":
                raise ConfigurationError("Health probes must use HTTPS")
            if not 100 <= int(probe["status"]) <= 599:
                raise ConfigurationError("Invalid expected HTTP status")
    if config.get("subscription_outbound") and config["subscription_outbound"] not in config["groups"]:
        raise ConfigurationError("subscription_outbound must name a managed group")
    for index in config.get("refresh_subscription_indices", []):
        if not isinstance(index, int) or index < 0 or index >= len(api.subs):
            raise ConfigurationError("A manual-refresh subscription index is out of range")
    if api.call("setting")["setting"]["pacMode"] != "routingA":
        raise ConfigurationError("Set up and verify RoutingA before installing; other routing modes are not migrated")
    version = subprocess.check_output(["docker", "exec", config["container"], "v2raya", "--version"], text=True, timeout=10).strip()
    if version != "2.2.7.4":
        raise ConfigurationError("This release supports v2rayA 2.2.7.4; validate a new version before using it")
    core = subprocess.check_output(["docker", "exec", config["container"], "xray", "version"], text=True, timeout=10).splitlines()[0]
    if not core.startswith("Xray "):
        raise ConfigurationError("An Xray core is required")


def planned_routing(config, api, previous):
    domains = {}
    suffixes = {}
    for name, group in config["groups"].items():
        hosts = {host_of(probe["url"]) for probe in group["probes"]}
        if group.get("require_country") or group.get("require_us"):
            hosts.add("www.cloudflare.com")
        for host in hosts:
            if host in domains and domains[host] != name:
                raise ConfigurationError("Two outbound groups claim the same health-probe host")
            domains[host] = name
        for host in group.get("routing_domains", []):
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or host.startswith("."):
                raise ConfigurationError("routing_domains must contain plain domain names")
            if host in suffixes and suffixes[host] != name:
                raise ConfigurationError("Two outbound groups claim the same routing domain")
            suffixes[host] = name
    subscription_outbound = config.get("subscription_outbound")
    if subscription_outbound:
        indices = set(config.get("refresh_subscription_indices", []))
        indices.update(i for group in config["groups"].values() for i in group["subscription_indices"])
        for index in indices:
            host = host_of(api.subs[index]["address"])
            if host in domains and domains[host] != subscription_outbound:
                raise ConfigurationError("Subscription host conflicts with a health-probe route")
            domains[host] = subscription_outbound
    lines = [BEGIN]
    for host, outbound in sorted(domains.items()):
        if any((host == suffix or host.endswith("." + suffix)) and other != outbound for suffix, other in suffixes.items()):
            raise ConfigurationError("A business routing domain conflicts with a health/subscription route")
        lines.append("inboundTag(http, socks, rule-http, rule-socks) && domain(full: " + host + ")->" + outbound)
    for host, outbound in sorted(suffixes.items()):
        lines.append("inboundTag(http, socks, rule-http, rule-socks) && domain(domain: " + host + ")->" + outbound)
    return "\n".join(lines + [END, ""]) + strip_managed(previous)


def systemctl(*args):
    subprocess.run(["systemctl", *args], check=True)


def require_root():
    if os.name != "posix" or os.geteuid() != 0:
        raise ConfigurationError("Run deployment commands as root on the target Linux server")


def inspect(config):
    api = watchdog.LocalAPI(config)
    result = {"outbounds": api.call("outbounds")["outbounds"], "running": api.call("touch")["running"],
              "subscriptions": [{"index": i, "nodes": len(s.get("servers", [])), "supported_cached_nodes": sum(watchdog.supported(n.get("serverObj", {})) for n in s.get("servers", []))} for i, s in enumerate(api.subs)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def restore_native(api, manifest):
    current = api.call("routingA")["routingA"]
    restored = strip_managed(current)
    if restored != current:
        api.call("routingA", "PUT", {"routingA": restored})
        watchdog.reload_core(api)
    setting = api.call("setting")["setting"]
    for key, value in manifest["applied_setting"].items():
        if setting.get(key) == value:
            setting[key] = manifest["previous_setting"][key]
    api.call("setting", "PUT", setting)
    if manifest.get("previous_running") and not api.call("touch")["running"]:
        api.call("v2ray", "POST")


def install(config, root, no_systemd=False, dry_run=False):
    require_root()
    if Path(root).resolve() != Path("/") and not no_systemd:
        raise ConfigurationError("A staging --root requires --no-systemd")
    paths = layout(root)
    if paths["manifest"].exists() or paths["service"].exists() or paths["runtime"].exists():
        raise ConfigurationError("An existing installation needs inspection/removal before reinstalling")
    api = watchdog.LocalAPI(config)
    validate(config, api)
    previous_setting = api.call("setting")["setting"]
    previous_routing = api.call("routingA")["routingA"]
    routing = planned_routing(config, api, previous_routing)
    updates = {"subscriptionAutoUpdateMode": "none"}
    if config.get("subscription_outbound"):
        updates["proxyModeWhenSubscribe"] = "pac"
    if dry_run:
        print(json.dumps({"action": "install_plan", "outbounds": list(config["groups"]), "automatic_subscription_updates": False,
                          "setting_changes": updates, "routingA": routing[:routing.index(END) + len(END)], "install_root": str(Path(root).resolve())}, ensure_ascii=False, indent=2))
        return
    installed = copy.deepcopy(config)
    installed.update(auth_file=str(paths["auth"]), state_file=str(paths["state"]), backup_dir=str(paths["backup"]), lock_file=str(paths["lock"]), subscription_refresh_seconds=0)
    for key in ("runtime", "config", "state", "lock"):
        paths[key].parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = watchdog.backup(installed, api)
    manifest = {"status": "preparing", "previous_setting": previous_setting, "applied_setting": updates, "previous_running": api.call("touch")["running"], "backup": str(before), "systemd": not no_systemd}
    watchdog.write_private(paths["manifest"], manifest)
    watchdog.write_private(paths["auth"], watchdog.read_private_json(config["auth_file"]))
    watchdog.write_private(paths["config"], installed)
    shutil.copyfile(Path(__file__).with_name("v2raya_failover.py"), paths["runtime"])
    paths["runtime"].chmod(0o700)
    try:
        api.call("routingA", "PUT", {"routingA": routing})
        api.call("setting", "PUT", {**previous_setting, **updates})
        if not watchdog.run(installed, {}, check_only=True):
            raise ConfigurationError("A managed route failed validation; previous routing will be restored")
        for name in ("service", "timer"):
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            content = (ASSETS / ("v2raya-failover." + name)).read_text(encoding="utf-8")
            paths[name].write_text(content, encoding="utf-8")
            paths[name].chmod(0o644)
        if not no_systemd:
            subprocess.run(["systemd-analyze", "verify", str(paths["service"]), str(paths["timer"])], check=True)
            systemctl("daemon-reload")
            systemctl("enable", "--now", "v2raya-failover.timer")
            systemctl("start", "v2raya-failover.service")
        manifest["status"] = "installed"
        watchdog.write_private(paths["manifest"], manifest)
        watchdog.log("installed", config=str(paths["config"]), automatic_subscription_updates=False, backup=str(before))
    except BaseException:
        if not no_systemd:
            subprocess.run(["systemctl", "disable", "--now", "v2raya-failover.timer"], capture_output=True)
            subprocess.run(["systemctl", "stop", "v2raya-failover.service"], capture_output=True)
        restore_native(api, manifest)
        manifest["status"] = "rolled_back"
        watchdog.write_private(paths["manifest"], manifest)
        raise


def uninstall(root, no_systemd=False):
    require_root()
    if Path(root).resolve() != Path("/") and not no_systemd:
        raise ConfigurationError("A staging --root requires --no-systemd")
    paths = layout(root)
    manifest = watchdog.read_private_json(paths["manifest"])
    config = watchdog.read_private_json(paths["config"])
    if not no_systemd:
        systemctl("disable", "--now", "v2raya-failover.timer")
        systemctl("stop", "v2raya-failover.service")
    api = watchdog.LocalAPI(config)
    watchdog.restore_subscription_override(api)
    restore_native(api, manifest)
    for key in ("service", "timer", "runtime"):
        paths[key].unlink(missing_ok=True)
    if not no_systemd:
        systemctl("daemon-reload")
    manifest["status"] = "uninstalled"
    watchdog.write_private(paths["manifest"], manifest)
    watchdog.log("uninstalled", retained_private_config_and_backups=True)


def save_auth(path, endpoint, token_only=False):
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "http" or not ipaddress.ip_address(parsed.hostname).is_loopback or parsed.username or parsed.password:
        raise ConfigurationError("Authentication must use the server's loopback HTTP management endpoint")
    if Path(path).exists():
        raise ConfigurationError("Auth file already exists; choose a new file and then replace it deliberately")
    username = input("v2rayA username: ")
    password = getpass.getpass("v2rayA password: ")
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/login", data=json.dumps({"username": username, "password": password}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
        result = json.load(response)
    if result.get("code") != "SUCCESS":
        raise ConfigurationError("v2rayA rejected the supplied login")
    value = {"token": result["data"]["token"]} if token_only else {"username": username, "password": password}
    watchdog.write_private(Path(path), value)
    watchdog.log("auth_saved", path=str(path), token_only=token_only)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Prompt locally and save a private official API credential")
    auth.add_argument("--auth-file", required=True)
    auth.add_argument("--api-url", default="http://127.0.0.1:2017")
    auth.add_argument("--token-only", action="store_true", help="Store an expiring token instead of login credentials")
    for name in ("inspect", "install"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "install":
            command.add_argument("--root", default="/")
            command.add_argument("--no-systemd", action="store_true", help="Stage files/routing without enabling a timer")
            command.add_argument("--dry-run", action="store_true")
    remove = sub.add_parser("uninstall", help="Disable service and remove managed rules; retain private backups")
    remove.add_argument("--root", default="/")
    remove.add_argument("--no-systemd", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "auth":
            save_auth(args.auth_file, args.api_url, args.token_only)
        elif args.command == "uninstall":
            uninstall(args.root, args.no_systemd)
        else:
            config = watchdog.read_private_json(args.config)
            if args.command == "inspect":
                inspect(config)
            else:
                install(config, args.root, args.no_systemd, args.dry_run)
    except ConfigurationError as error:
        print("Configuration error: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        # Native HTTP errors and subprocess arguments can contain private data.
        print("Operation failed (" + type(error).__name__ + "). Inspect the local configuration and protected backups.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
