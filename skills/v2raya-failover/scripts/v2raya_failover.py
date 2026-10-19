#!/usr/bin/env python3
"""Server-local v2rayA 2.2.7.4/Xray failover; Python standard library only.

Reads the root-only Bolt database for cached nodes. Authentication uses the
official login API or an operator-provided token. All mutations use the API.
Subscription URLs, credentials, response bodies and JWTs must not be logged.
"""

import argparse
import base64
import concurrent.futures
import contextlib
import copy
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def read_bolt(path):
    """Read committed Bolt leaf records, retrying around concurrent writes."""
    for attempt in range(4):
        try:
            data = Path(path).read_bytes()
            size = struct.unpack_from("<I", data, 24)[0]
            if size not in (4096, 8192, 16384, 32768, 65536):
                raise ValueError("unsupported Bolt page size")
            metas = [data[:size], data[size:2 * size]]
            meta = max(metas, key=lambda x: struct.unpack_from("<Q", x, 64)[0])
            root = struct.unpack_from("<Q", meta, 32)[0]
            records = {}

            def walk(page, path, depth=0):
                if depth > 32:
                    raise ValueError("invalid Bolt tree")
                flags, count = struct.unpack_from("<HH", page, 8)
                if flags & 1:
                    for i in range(count):
                        _, _, pgid = struct.unpack_from("<IIQ", page, 16 + i * 16)
                        walk(data[pgid * size:], path, depth + 1)
                elif flags & 2:
                    for i in range(count):
                        off = 16 + i * 16
                        fl, pos, ks, vs = struct.unpack_from("<IIII", page, off)
                        key = page[off + pos:off + pos + ks].decode()
                        val = page[off + pos + ks:off + pos + ks + vs]
                        if len(val) != vs:
                            raise ValueError("incomplete Bolt record")
                        if fl & 1:
                            pgid = struct.unpack_from("<Q", val)[0]
                            walk(data[pgid * size:] if pgid else val[16:], path + (key,), depth + 1)
                        else:
                            records[path + (key,)] = val
                else:
                    raise ValueError("invalid Bolt page")

            walk(data[root * size:], ())
            # An advancing metadata page means the copied pages may span commits.
            with open(path, "rb") as stream:
                current = stream.read(2 * size)
            txid = max(struct.unpack_from("<Q", current, off + 64)[0] for off in (0, size))
            if txid != struct.unpack_from("<Q", meta, 64)[0]:
                raise ValueError("database changed during read")
            return records
        except (ValueError, struct.error, UnicodeError, IndexError):
            if attempt == 3:
                raise
            time.sleep(0.1)


class LocalAPI:
    def __init__(self, config):
        self.config = config
        endpoint = urllib.parse.urlsplit(config["api_url"])
        if endpoint.scheme != "http" or endpoint.username or endpoint.password or not ipaddress.ip_address(endpoint.hostname).is_loopback:
            raise ValueError("management API must use a loopback HTTP address")
        self.token = ""
        auth = read_private_json(config["auth_file"])
        if auth.get("token"):
            self.token = auth["token"]
        else:
            result = self.call("login", "POST", {"username": auth["username"], "password": auth["password"]})
            self.token = result["token"]
        self.reload()

    def reload(self):
        self.records = read_bolt(Path(self.config["data_dir"]) / "bolt.db")
        self.subs = json.loads(self.records[("touch", "subscriptions")])

    def call(self, path, method="GET", body=None):
        request = urllib.request.Request(
            self.config["api_url"].rstrip("/") + "/api/" + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.token} if self.token else {})},
        )
        # Local management requests must never inherit system HTTP_PROXY settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=45) as response:
            result = json.load(response)
        if result.get("code") != "SUCCESS":
            raise RuntimeError("v2rayA API rejected " + path.split("?")[0])
        return result.get("data")


def read_private_json(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("private files must not be symlinks")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
            raise PermissionError("private file must be owned by the current user with mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def curl(url, proxy, timeout=8):
    """Pass URLs via stdin so subscription tokens never appear in process args."""
    args = ["curl", "-sS", "-L", "--proto", "=https", "--proto-redir", "=https",
            "--connect-timeout", "4", "--max-time", str(timeout), "--max-filesize", "2097152",
            "-A", "v2rayA/2.2.7.4 WebRequestHelper", "--config", "-",
            "-w", "\nWATCHDOG %{http_code} %{time_total}"]
    args += ["--proxy", proxy, "--noproxy", ""] if proxy else ["--noproxy", "*"]
    result = subprocess.run(args, input="url = " + json.dumps(url) + "\n", text=True, capture_output=True, timeout=timeout + 3)
    body, _, metrics = result.stdout.rpartition("\nWATCHDOG ")
    parts = metrics.split()
    return result.returncode, parts[0] if parts else "000", body, float(parts[1]) if len(parts) > 1 else timeout


def healthy(proxy, group):
    total = 0.0
    for probe in group["probes"]:
        rc, status, body, elapsed = curl(probe["url"], proxy)
        total += elapsed
        if rc or status != str(probe["status"]) or (probe.get("contains") and probe["contains"] not in body):
            return False, total
    country = group.get("require_country") or ("US" if group.get("require_us") else None)
    if country:
        rc, status, body, elapsed = curl("https://www.cloudflare.com/cdn-cgi/trace", proxy)
        total += elapsed
        if rc or status != "200" or "loc=" + country not in body.splitlines():
            return False, total
    return True, total


def node_key(node):
    return tuple(str(node.get(k, "")) for k in ("protocol", "add", "port", "id", "pbk", "sid", "sni", "flow", "net", "tls"))


def supported(node):
    return node.get("protocol") == "vless" and node.get("net", "tcp") == "tcp" and node.get("tls") == "reality" and all(node.get(k) for k in ("add", "port", "id", "pbk", "sni"))


def isolated_config(node, port):
    if not supported(node):
        raise ValueError("unsupported cached node transport")
    outbound = {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": node["add"], "port": int(node["port"]),
                                  "users": [{"id": node["id"], "encryption": "none", "flow": node.get("flow", "")}]}]},
        "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
            "serverName": node["sni"], "fingerprint": node.get("fingerprint") or "chrome",
            "publicKey": node["pbk"], "shortId": node.get("sid", "")}},
    }
    return {"log": {"loglevel": "none"}, "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {}}], "outbounds": [outbound]}


@contextlib.contextmanager
def isolated_proxy(config, node):
    pid = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Pid}}", config["container"]], text=True, timeout=10).strip()
    executable = f"/proc/{int(pid)}/root/usr/local/bin/xray"
    with tempfile.TemporaryDirectory(prefix="v2raya-failover-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(isolated_config(node, port)))
        path.chmod(0o600)
        process = subprocess.Popen([executable, "run", "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            while True:
                if process.poll() is not None:
                    raise RuntimeError("isolated Xray did not start")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("isolated Xray did not listen")
                    time.sleep(0.05)
            yield "http://127.0.0.1:" + str(port)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def best_candidate(api, group, current):
    candidates = []
    for subindex in group["subscription_indices"]:
        for index, raw in enumerate(api.subs[subindex].get("servers", []), 1):
            node = raw.get("serverObj", {})
            if supported(node) and any(node.get("ps", "").startswith(p) for p in group["name_prefixes"]):
                which = {"_type": "subscriptionServer", "sub": subindex, "id": index, "outbound": current["outbound"]}
                if (subindex, index) != (current.get("sub"), current.get("id")):
                    candidates.append((which, node))

    def tcp(item):
        start = time.monotonic()
        try:
            with socket.create_connection((item[1]["add"], int(item[1]["port"])), timeout=2):
                return time.monotonic() - start, item
        except OSError:
            return float("inf"), item

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        ranked = sorted(pool.map(tcp, candidates), key=lambda x: x[0])
    shortlist = [item for delay, item in ranked if delay != float("inf")][:6]

    def check(item):
        try:
            with isolated_proxy(api.config, item[1]) as proxy:
                ok, elapsed = healthy(proxy, group)
                return elapsed if ok else float("inf"), item
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return float("inf"), item

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        # If the six fastest TCP endpoints fail HTTP, keep searching the cached
        # list in bounded batches instead of depending on a subscription refresh.
        while shortlist:
            results = sorted(pool.map(check, shortlist), key=lambda x: x[0])
            if results[0][0] != float("inf"):
                return results[0][1]
            ranked = ranked[6:]
            shortlist = [item for delay, item in ranked if delay != float("inf")][:6]
    return None


def backup(config, api):
    root = Path(config["backup_dir"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns()))
    path.mkdir(mode=0o700)
    for name in ("bolt.db", "config.json"):
        shutil.copy2(Path(config["data_dir"]) / name, path / name)
        (path / name).chmod(0o600)
    (path / "before-api.json").write_text(json.dumps({"touch": api.call("touch")["touch"]["connectedServer"], "setting": api.call("setting"), "routingA": api.call("routingA")}))
    (path / "before-api.json").chmod(0o600)
    return path


def guard_group(api, name, group, state, check_only=False):
    proxy = api.config["proxy_url"]
    ok, _ = healthy(proxy, group)
    count = 0 if ok else state.get("failures", 0) + 1
    state["failures"] = count
    if ok or check_only:
        return ok
    log("probe_failed", outbound=name, consecutive=count)
    if count < api.config.get("failure_threshold", 2):
        return False
    connected = api.call("touch")["touch"]["connectedServer"]
    current = next((x for x in connected if x.get("outbound") == name), None)
    if current is None:
        log("outbound_not_selected", outbound=name)
        return False
    api.reload()
    candidate = best_candidate(api, group, current)
    if candidate is None:
        log("no_healthy_cached_candidate", outbound=name)
        return False
    which, node = candidate
    # Subscription updates may reorder IDs while probes are in flight.
    api.reload()
    if node_key(api.subs[which["sub"]]["servers"][which["id"] - 1]["serverObj"]) != node_key(node):
        log("candidate_changed_retry_next_run", outbound=name)
        return False
    latest = next((x for x in api.call("touch")["touch"]["connectedServer"] if x.get("outbound") == name), None)
    if latest != current or healthy(proxy, group)[0]:
        log("recovered_or_manually_changed", outbound=name)
        return True
    before = backup(api.config, api)
    try:
        api.call("connection", "POST", which)
        for port in api.config["verify_ports"]:
            if not healthy("http://127.0.0.1:" + str(port), group)[0]:
                raise RuntimeError("switched outbound did not pass validation")
        state.update(failures=0, last_switch=time.time())
        log("switched", outbound=name, subscription_index=which["sub"], node_id=which["id"], backup=str(before))
        return True
    except Exception:
        api.call("connection", "POST", current)
        log("switch_rolled_back", outbound=name, backup=str(before))
        raise


def subscription_nodes(body):
    raw = body.strip().lstrip("\ufeff")
    prefixes = ("vless://", "vmess://", "trojan://", "ss://", "ssr://")
    if not any(line.startswith(prefixes) for line in raw.splitlines()):
        compact = "".join(raw.split())
        try:
            raw = base64.b64decode(compact + "=" * (-len(compact) % 4), altchars=b"-_", validate=True).decode()
        except (ValueError, UnicodeError):
            return []
    lines = [line for line in raw.splitlines() if line.startswith(prefixes)]
    # Reject HTML/error pages or implausibly empty responses before native update.
    return lines if len(lines) >= 5 and "<html" not in raw.lower() else []


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".private-", delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reload_core(api):
    # PUT routingA only saves text in 2.2.7.4. Reapply the unchanged outbound
    # settings to regenerate the running core without overwriting global settings.
    # This endpoint also remains available if a subscription request is stuck.
    setting = api.call("outbound?outbound=proxy")["setting"]
    api.call("outbound", "PUT", {"outbound": "proxy", "setting": setting})


def restore_subscription_override(api):
    path = Path(api.config["state_file"]).with_suffix(".subscription-restore.json")
    if not path.exists():
        return
    pending = json.loads(path.read_text())
    if pending["applied_setting"] is not None and api.call("setting")["setting"] == pending["applied_setting"]:
        api.call("setting", "PUT", pending["setting"])
    if pending["applied_routing"] is not None:
        current = api.call("routingA")["routingA"]
        if current == pending["applied_routing"]:
            api.call("routingA", "PUT", {"routingA": pending["routing"]})
        if current in (pending["applied_routing"], pending["routing"]):
            reload_core(api)
    path.unlink()
    log("subscription_override_restored")


def refresh_subscriptions(api, state, force=False):
    interval = api.config.get("subscription_refresh_seconds", 0)
    # Subscription rotation is a provider policy, independent of node failover.
    # Zero (and an omitted setting) means manual-only, including during faults.
    if not force and interval <= 0:
        return
    if not force and time.time() - state.get("subscription_attempt", 0) < min(interval, 900):
        return
    if not force and time.time() - state.get("subscription_success", 0) < interval:
        return
    state["subscription_attempt"] = time.time()
    api.reload()
    for index in api.config["refresh_subscription_indices"]:
        url = api.subs[index]["address"]
        rc, status, body, _ = curl(url, api.config["proxy_url"], timeout=15)
        nodes = subscription_nodes(body)
        mode = "pac"
        if rc or status != "200" or not nodes:
            # If the Hong Kong cache is exhausted, try direct access and then
            # the US node through an isolated core, bypassing the broken route.
            rc, status, body, _ = curl(url, "", timeout=10)
            nodes = subscription_nodes(body)
            mode = "direct"
        if rc or status != "200" or not nodes:
            mode = "primary"
            current = next((x for x in api.call("touch")["touch"]["connectedServer"] if x.get("outbound") == "proxy"), None)
            if current and current.get("_type") == "subscriptionServer":
                node = api.subs[current["sub"]]["servers"][current["id"] - 1]["serverObj"]
                if supported(node):
                    with isolated_proxy(api.config, node) as proxy:
                        rc, status, body, _ = curl(url, proxy, timeout=15)
                        nodes = subscription_nodes(body)
        if rc or status != "200" or not nodes:
            log("subscription_preflight_failed_keep_cache", subscription=index + 1, status=status)
            return
        backup(api.config, api)
        old_setting = api.call("setting")["setting"]
        old_routing = api.call("routingA")["routingA"]
        changed_setting = {**old_setting, "proxyModeWhenSubscribe": "direct"} if mode == "direct" else None
        host = urllib.parse.urlsplit(url).hostname
        changed_routing = "domain(full: " + host + ")->proxy\n" + old_routing if mode == "primary" else None
        if changed_setting is not None or changed_routing is not None:
            # Persist intent before changing routing. A killed service can restore
            # its temporary override on the next run without reverting an operator.
            write_private(Path(api.config["state_file"]).with_suffix(".subscription-restore.json"), {
                "setting": old_setting, "routing": old_routing,
                "applied_setting": changed_setting, "applied_routing": changed_routing,
            })
        try:
            if changed_setting is not None:
                api.call("setting", "PUT", changed_setting)
            elif changed_routing is not None:
                api.call("routingA", "PUT", {"routingA": changed_routing})
                reload_core(api)
            api.call("subscription", "PUT", {"_type": "subscription", "id": index + 1})
        finally:
            restore_subscription_override(api)
        api.reload()
        if len(api.subs[index].get("servers", [])) < 5:
            raise RuntimeError("subscription node count unexpectedly small; inspect backup")
        log("subscription_updated", subscription=index + 1, nodes=len(api.subs[index]["servers"]), path=mode)
    state["subscription_success"] = time.time()


def run(config, state, check_only=False, refresh=False):
    api = LocalAPI(config)
    if not check_only:
        restore_subscription_override(api)
    touch = api.call("touch")
    if not touch.get("running"):
        log("core_stopped_no_action")
        return False
    all_healthy = True
    for name, group in config["groups"].items():
        group_state = state.setdefault(name, {})
        all_healthy = guard_group(api, name, group, group_state, check_only) and all_healthy
    # Refresh must still work when one outbound has no surviving cached nodes.
    if not check_only:
        refresh_subscriptions(api, state, force=refresh)
    log("check_complete", healthy=all_healthy, check_only=check_only)
    return all_healthy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/v2raya-failover/config.json")
    parser.add_argument("--check", action="store_true", help="Read-only end-to-end checks")
    parser.add_argument("--refresh", action="store_true", help="Refresh managed subscription using an available validated path")
    args = parser.parse_args()
    config = read_private_json(args.config)
    # Lock and state are private and on a local filesystem. systemd also prevents
    # overlapping service instances; flock covers manually invoked copies.
    import fcntl
    with open(config["lock_file"], "a") as lock:
        os.chmod(config["lock_file"], 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = Path(config["state_file"])
        state = json.loads(path.read_text()) if path.exists() else {}
        try:
            result = run(config, state, args.check, args.refresh)
            if args.check and not result:
                raise SystemExit(1)
        except Exception as error:
            # Exception strings from urllib/curl can contain subscription URLs.
            log("run_error", kind=type(error).__name__)
            raise SystemExit(1) from None
        finally:
            if not args.check:
                write_private(path, state)


if __name__ == "__main__":
    main()
