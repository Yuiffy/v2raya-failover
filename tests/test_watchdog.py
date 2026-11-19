import base64
import copy
import contextlib
from pathlib import Path
import tempfile
import unittest
import urllib.error
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/v2raya-failover/scripts"))
import v2raya_failover as watchdog


class FailoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.current = {"_type": "subscriptionServer", "id": 1, "sub": 0, "outbound": "proxy"}
        self.node = {"protocol": "vless", "net": "tcp", "tls": "reality", "add": "node.invalid", "port": 443,
                     "id": "test-id", "pbk": "test-public-key", "sni": "example.org"}
        self.api = Mock()
        self.api.config = {"container": "v2raya", "proxy_url": "http://127.0.0.1:20172", "failure_threshold": 2, "verify_ports": [20171, 20172],
                           "state_file": str(Path(temporary.name) / "state.json")}
        self.api.subs = [{"servers": [{"serverObj": {}}, {"serverObj": self.node}]}]
        self.api.call.return_value = {"touch": {"connectedServer": [self.current]}}
        self.which = {**self.current, "id": 2}

    def shadowsocks_node(self):
        return {
            "protocol": "shadowsocks", "name": "🇺🇸 US cached node",
            "server": "ss.invalid", "port": 8443, "cipher": "aes-128-gcm",
            "password": "test-password", "plugin": {
                "name": "simple-obfs", "opts": {"obfs": "http", "host": "cdn.invalid", "uri": "probe"},
            },
        }

    def test_shadowsocks_obfs_uses_cached_credentials_and_transport(self):
        node = self.shadowsocks_node()
        self.assertTrue(watchdog.supported(node))
        config = watchdog.isolated_config(node, 24300)
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["settings"]["servers"][0], {
            "address": "ss.invalid", "port": 8443, "method": "aes-128-gcm", "password": "test-password",
        })
        header = outbound["streamSettings"]["tcpSettings"]["header"]
        self.assertEqual(header["request"]["headers"]["Host"], ["cdn.invalid"])
        self.assertEqual(header["request"]["path"], ["/probe"])
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")

    def test_plain_shadowsocks_and_unsupported_plugins(self):
        node = self.shadowsocks_node()
        node.pop("plugin")
        self.assertTrue(watchdog.supported(node))
        self.assertNotIn("streamSettings", watchdog.isolated_config(node, 24300)["outbounds"][0])
        for change in [
            {"cipher": "rc4-md5"},
            {"password": ""},
            {"plugin": {"name": "v2ray-plugin"}},
            {"plugin": {"name": "simple-obfs", "opts": {"obfs": "tls"}}},
        ]:
            with self.subTest(change=change):
                invalid = {**node, **change}
                self.assertFalse(watchdog.supported(invalid))
                with self.assertRaises(ValueError):
                    watchdog.isolated_config(invalid, 24300)

    def test_shadowsocks_identity_detects_password_or_plugin_changes(self):
        node = self.shadowsocks_node()
        key = watchdog.node_key(node)
        self.assertEqual(key, watchdog.node_key({**node, "name": "renamed"}))
        self.assertNotEqual(key, watchdog.node_key({**node, "password": "changed"}))
        changed = copy.deepcopy(node)
        changed["plugin"]["opts"]["host"] = "changed.invalid"
        self.assertNotEqual(key, watchdog.node_key(changed))

    @patch.object(watchdog, "healthy", return_value=(True, 1))
    @patch.object(watchdog, "isolated_proxy", return_value=contextlib.nullcontext("http://127.0.0.1:24300"))
    @patch.object(watchdog.socket, "create_connection", side_effect=lambda *args, **kwargs: contextlib.nullcontext())
    def test_shadowsocks_candidate_uses_name_server_fields_and_region_filter(self, connect, isolate, _):
        node = self.shadowsocks_node()
        self.api.subs = [{"servers": [{"serverObj": {}}, {"serverObj": node}]}]
        group = {"subscription_indices": [0], "name_prefixes": ["🇺🇸"]}
        candidate = watchdog.best_candidate(self.api, group, self.current)
        self.assertEqual(candidate, (self.which, node))
        connect.assert_called_once_with(("ss.invalid", 8443), timeout=2)
        isolate.assert_called_once_with(self.api.config, node)
        self.assertEqual(watchdog.named_node(self.api, self.which)["name"], node["name"])
        group["name_prefixes"] = ["🇯🇵"]
        self.assertIsNone(watchdog.best_candidate(self.api, group, self.current))

    @patch.object(watchdog, "healthy", return_value=(True, 1))
    def test_healthy_node_clears_failures_without_switch(self, _):
        state = {"failures": 1}
        self.assertTrue(watchdog.guard_group(self.api, "proxy", {}, state))
        self.assertEqual(state["failures"], 0)
        self.api.call.assert_not_called()

    @patch.object(watchdog, "healthy", return_value=(False, 8))
    def test_first_failure_does_not_switch(self, _):
        state = {}
        self.assertFalse(watchdog.guard_group(self.api, "proxy", {}, state))
        self.assertEqual(state["failures"], 1)
        self.api.call.assert_not_called()

    @patch.object(watchdog, "business_probe", side_effect=[False, None])
    @patch.object(watchdog, "healthy", return_value=(True, 1))
    def test_business_timeout_starts_count_and_probe_error_resets_it(self, *_):
        state = {}
        group = {"business_probe": {"command": ["/service-probe"]}}
        self.assertFalse(watchdog.guard_group(self.api, "proxy", group, state))
        self.assertEqual(state["business_failures"], 1)
        self.assertFalse(watchdog.guard_group(self.api, "proxy", group, state))
        self.assertEqual(state["business_failures"], 0)
        self.api.call.assert_not_called()

    @patch.object(watchdog, "backup", return_value="backup")
    @patch.object(watchdog, "curl", return_value=(0, "200", "ip=203.0.113.1\n", 1))
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy", return_value=(True, 1))
    def test_business_trial_keeps_only_a_successful_application_route(self, _, candidate, __, ___):
        group = {"business_probe": {"command": ["/service-probe"]}}
        candidate.return_value = self.which, self.node
        for recovered in (True, False):
            with self.subTest(recovered=recovered):
                selected = [self.current]
                self.api.call.return_value["touch"]["connectedServer"] = selected

                def replace(_, node):
                    selected[:] = [node]

                state = {"business_failures": 1}
                with patch.object(watchdog, "replace_connection", side_effect=replace), patch.object(
                    watchdog, "business_probe", side_effect=[False, False, recovered]
                ):
                    if recovered:
                        self.assertTrue(watchdog.guard_group(self.api, "proxy", group, state))
                    else:
                        with self.assertRaisesRegex(RuntimeError, "business probe"):
                            watchdog.guard_group(self.api, "proxy", group, state)

                self.assertEqual(selected, [self.which if recovered else self.current])
                self.assertFalse(watchdog.trial_path(self.api.config, "proxy").exists())
                self.assertEqual(state["business_failures"], 0)
                self.assertGreater(state["last_business_trial"], 0)
        self.assertEqual(str(candidate.call_args.kwargs["exclude_exit"]), "203.0.113.1")

    def test_interrupted_business_trial_is_rolled_back_before_retry(self):
        path = watchdog.trial_path(self.api.config, "proxy")
        watchdog.write_private(path, {"previous": self.current, "candidate": self.which, "started_at": 12})
        state = {}
        with patch.object(watchdog, "outbound_selection", side_effect=[[self.which], [self.current]]), patch.object(
            watchdog, "replace_connection"
        ) as replace:
            watchdog.restore_business_trial(self.api, "proxy", state)
        replace.assert_called_once_with(self.api, self.current)
        self.assertEqual(state["last_business_trial"], 12)
        self.assertFalse(path.exists())

    @patch.object(watchdog, "business_probe", return_value=False)
    @patch.object(watchdog, "healthy", return_value=(True, 1))
    @patch.object(watchdog.time, "time", return_value=606)
    def test_business_trial_cooldown_blocks_an_immediate_second_trial(self, *_):
        state = {"business_failures": 1, "last_business_trial": 12}
        group = {"business_probe": {"command": ["/service-probe"]}}
        self.assertFalse(watchdog.guard_group(self.api, "proxy", group, state))
        self.api.call.assert_not_called()

    @patch.object(watchdog, "healthy", return_value=(False, 8))
    def test_check_only_never_mutates_api(self, _):
        self.assertFalse(watchdog.guard_group(self.api, "proxy", {}, {"failures": 10}, check_only=True))
        self.api.call.assert_not_called()

    @patch.object(watchdog, "backup", return_value="backup")
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy")
    def test_second_failure_switches_only_failed_group(self, health, candidate, _):
        health.side_effect = [(False, 8), (False, 8), (True, 1), (True, 1)]
        candidate.return_value = self.which, self.node
        state = {"failures": 1}
        self.assertTrue(watchdog.guard_group(self.api, "proxy", {}, state))
        writes = [call for call in self.api.call.call_args_list if len(call.args) > 1]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args, ("outboundConnections", "PUT", {"outbound": "proxy", "touches": [self.which]}))
        self.assertEqual(state["failures"], 0)

    @patch.object(watchdog, "backup", return_value="backup")
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy")
    def test_switch_validation_failure_restores_original(self, health, candidate, _):
        health.side_effect = [(False, 8), (False, 8), (False, 8)]
        candidate.return_value = self.which, self.node
        with self.assertRaises(RuntimeError):
            watchdog.guard_group(self.api, "proxy", {}, {"failures": 1})
        self.assertEqual(self.api.call.call_args.args, ("outboundConnections", "PUT", {"outbound": "proxy", "touches": [self.current]}))

    @patch.object(watchdog, "best_candidate", return_value=None)
    @patch.object(watchdog, "healthy", return_value=(False, 8))
    def test_all_cached_nodes_failed_does_not_replace_selection(self, *_):
        self.assertFalse(watchdog.guard_group(self.api, "proxy", {}, {"failures": 1}))
        self.assertTrue(all(len(call.args) == 1 for call in self.api.call.call_args_list))

    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy", return_value=(False, 8))
    def test_reordered_subscription_candidate_is_not_connected(self, _, candidate):
        candidate.return_value = self.which, {**self.node, "id": "old-id"}
        self.assertFalse(watchdog.guard_group(self.api, "proxy", {}, {"failures": 1}))
        self.assertTrue(all(len(call.args) == 1 for call in self.api.call.call_args_list))

    def test_plain_and_base64_subscriptions_and_html(self):
        body = "\n".join("vless://test@node.invalid:443#US" for _ in range(5))
        self.assertEqual(len(watchdog.subscription_nodes(body)), 5)
        self.assertEqual(len(watchdog.subscription_nodes(base64.b64encode(body.encode()).decode())), 5)
        self.assertEqual(watchdog.subscription_nodes("<html>Access denied</html>"), [])
        self.assertEqual(watchdog.subscription_nodes("vless://only-one"), [])

    def test_legacy_missing_replacement_endpoint_uses_connection(self):
        for status in (404, 405):
            with self.subTest(status=status):
                self.api.call.reset_mock()
                self.api.call.side_effect = [
                    urllib.error.HTTPError("http://127.0.0.1/api/outboundConnections", status, "missing", {}, None),
                    {},
                    {"touch": {"connectedServer": [self.which]}},
                    {"touch": {"connectedServer": [self.which]}},
                ]
                watchdog.replace_connection(self.api, self.which)
                self.api.call.assert_any_call("connection", "POST", self.which)

    def test_legacy_replacement_removes_old_members_only_from_its_group(self):
        other = {**self.current, "outbound": "downloads"}
        self.api.call.side_effect = [
            {},
            {"touch": {"connectedServer": [self.current, self.which, other]}},
            {},
            {"touch": {"connectedServer": [self.which, other]}},
        ]
        watchdog.legacy_replace_connection(self.api, self.which)
        self.api.call.assert_any_call("connection", "DELETE", self.current)
        self.assertFalse(any(call.args == ("connection", "DELETE", other) for call in self.api.call.call_args_list))

    def test_replacement_auth_and_server_errors_do_not_use_legacy_endpoint(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                self.api.call.reset_mock()
                self.api.call.side_effect = urllib.error.HTTPError(
                    "http://127.0.0.1/api/outboundConnections", status, "rejected", {}, None
                )
                with self.assertRaises(urllib.error.HTTPError):
                    watchdog.replace_connection(self.api, self.which)
                self.api.call.assert_called_once()

    @patch.object(watchdog, "curl", return_value=(0, "200", "<html>error</html>", 1))
    def test_invalid_subscription_never_calls_update(self, _):
        self.api.config.update(refresh_subscription_indices=[0], subscription_refresh_seconds=21600)
        self.api.subs[0]["address"] = "https://subscription.invalid/private"
        watchdog.refresh_subscriptions(self.api, {}, force=True)
        self.assertTrue(all(len(call.args) == 1 for call in self.api.call.call_args_list))

    @patch.object(watchdog, "curl")
    def test_periodic_updates_disabled_even_without_previous_success(self, fetch):
        for interval in [0, -1, None]:
            if interval is None:
                self.api.config.pop("subscription_refresh_seconds", None)
            else:
                self.api.config["subscription_refresh_seconds"] = interval
            state = {}
            watchdog.refresh_subscriptions(self.api, state)
            self.assertEqual(state, {})
        fetch.assert_not_called()
        self.api.reload.assert_not_called()
        self.api.call.assert_not_called()

    @patch.object(watchdog, "backup", return_value="backup")
    @patch.object(watchdog, "curl")
    def test_direct_subscription_fallback_restores_setting_on_failure(self, fetch, _):
        body = "\n".join("vless://test@node.invalid:443#US" for _ in range(5))
        fetch.side_effect = [(28, "000", "", 8), (0, "200", body, 1)]
        self.api.config.update(refresh_subscription_indices=[0])
        self.api.subs[0]["address"] = "https://subscription.invalid/private"
        setting = {"proxyModeWhenSubscribe": "pac"}

        def call(path, method="GET", value=None):
            if path == "setting":
                if method == "PUT":
                    setting.clear()
                    setting.update(value)
                return {"setting": dict(setting)}
            if path == "routingA":
                return {"routingA": "default: proxy\n"}
            if path == "subscription":
                raise RuntimeError("simulated update failure")

        self.api.call.side_effect = call
        with self.assertRaises(RuntimeError):
            watchdog.refresh_subscriptions(self.api, {}, force=True)
        self.assertEqual(setting, {"proxyModeWhenSubscribe": "pac"})

    @patch.object(watchdog, "backup", return_value="backup")
    @patch.object(watchdog, "reload_core")
    @patch.object(watchdog, "isolated_proxy", return_value=contextlib.nullcontext("http://isolated"))
    @patch.object(watchdog, "curl")
    def test_primary_subscription_fallback_restores_hk_route(self, fetch, *_):
        body = "\n".join("vless://test@node.invalid:443#US" for _ in range(5))
        fetch.side_effect = [(28, "000", "", 8), (0, "403", "denied", 1), (0, "200", body, 1)]
        self.api.config.update(refresh_subscription_indices=[0])
        self.api.subs[0].update(address="https://subscription.invalid/private", servers=[{"serverObj": self.node}] * 5)
        routing = ["domain(full: subscription.invalid)->dockerhk\n"]
        original = routing[0]

        def call(path, method="GET", value=None):
            if path == "touch":
                return {"touch": {"connectedServer": [self.current]}}
            if path == "setting":
                return {"setting": {"proxyModeWhenSubscribe": "pac"}}
            if path == "routingA":
                if method == "PUT":
                    routing[0] = value["routingA"]
                return {"routingA": routing[0]}

        self.api.call.side_effect = call
        state = {}
        watchdog.refresh_subscriptions(self.api, state, force=True)
        self.assertEqual(routing[0], original)
        self.assertIn("subscription_success", state)

    @patch.object(watchdog, "refresh_subscriptions")
    @patch.object(watchdog, "restore_subscription_override")
    @patch.object(watchdog, "guard_group", return_value=False)
    @patch.object(watchdog, "LocalAPI")
    def test_subscription_can_refresh_with_exhausted_outbound(self, api_class, _, restore, refresh):
        api_class.return_value.call.return_value = {"running": True, "touch": {"connectedServer": []}}
        watchdog.run({"groups": {"proxy": {}}}, {})
        refresh.assert_called_once()

    @patch.object(watchdog, "log")
    @patch.object(watchdog, "guard_group", return_value=True)
    @patch.object(watchdog, "LocalAPI")
    def test_read_only_check_requires_a_matching_alternative(self, api_class, _, log):
        self.api.call.return_value["running"] = True
        node = self.shadowsocks_node()
        self.api.subs[0]["servers"][0]["serverObj"] = node
        api_class.return_value = self.api
        group = {"subscription_indices": [0], "name_prefixes": ["🇺🇸"]}
        config = {"groups": {"proxy": group}}

        self.assertFalse(watchdog.run(config, {}, check_only=True))
        log.assert_any_call("candidate_pool_unavailable", outbound="proxy", selected_nodes=1)
        log.assert_called_with("check_complete", healthy=True, candidate_pool_ready=False, check_only=True)

        self.api.subs[0]["servers"][1]["serverObj"]["ps"] = "🇺🇸 alternate"
        self.assertTrue(watchdog.run(config, {}, check_only=True))
        self.assertTrue(all(call.args == ("touch",) for call in self.api.call.call_args_list))

    @patch.object(watchdog, "reload_core")
    def test_interrupted_subscription_override_is_recovered(self, reload):
        path = Path(self.api.config["state_file"]).with_suffix(".subscription-restore.json")
        watchdog.write_private(path, {"setting": {}, "routing": "original", "applied_setting": None, "applied_routing": "temporary"})
        self.api.call.return_value = {"routingA": "temporary"}
        watchdog.restore_subscription_override(self.api)
        self.api.call.assert_called_with("routingA", "PUT", {"routingA": "original"})
        reload.assert_called_once_with(self.api)
        self.assertFalse(path.exists())

    def test_transport_is_not_silently_downgraded(self):
        node = copy.deepcopy(self.node)
        node["net"] = "ws"
        self.assertFalse(watchdog.supported(node))
        with self.assertRaises(ValueError):
            watchdog.isolated_config(node, 24300)

    def test_bolt_cache_falls_back_from_sqlite_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bolt.db").write_bytes(b"SQLite format 3\x00" + b"x" * 32)
            (root / "bolt.db.bak").write_bytes(b"bolt-cache")
            self.assertEqual(watchdog.bolt_database_path(root), root / "bolt.db.bak")

    @patch.object(watchdog.subprocess, "check_output", return_value="123\n")
    @patch.object(watchdog.Path, "is_file", return_value=True)
    @patch.object(watchdog.subprocess, "Popen", side_effect=FileNotFoundError)
    def test_isolated_proxy_prefers_v2raya_core_path(self, popen, _, inspect):
        with self.assertRaises(FileNotFoundError):
            watchdog.isolated_proxy(self.api.config, self.node).__enter__()
        self.assertEqual(Path(popen.call_args.args[0][0]).parts[-3:], ("usr", "bin", "v2raya_core"))


if __name__ == "__main__":
    unittest.main()
