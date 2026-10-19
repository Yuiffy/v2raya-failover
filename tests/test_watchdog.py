import base64
import copy
import contextlib
from pathlib import Path
import tempfile
import unittest
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
        self.api.config = {"proxy_url": "http://127.0.0.1:20172", "failure_threshold": 2, "verify_ports": [20171, 20172],
                           "state_file": str(Path(temporary.name) / "state.json")}
        self.api.subs = [{"servers": [{"serverObj": {}}, {"serverObj": self.node}]}]
        self.api.call.return_value = {"touch": {"connectedServer": [self.current]}}
        self.which = {**self.current, "id": 2}

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
        self.assertEqual(writes[0].args, ("connection", "POST", self.which))
        self.assertEqual(state["failures"], 0)

    @patch.object(watchdog, "backup", return_value="backup")
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy")
    def test_switch_validation_failure_restores_original(self, health, candidate, _):
        health.side_effect = [(False, 8), (False, 8), (False, 8)]
        candidate.return_value = self.which, self.node
        with self.assertRaises(RuntimeError):
            watchdog.guard_group(self.api, "proxy", {}, {"failures": 1})
        self.assertEqual(self.api.call.call_args.args, ("connection", "POST", self.current))

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
        api_class.return_value.call.return_value = {"running": True}
        watchdog.run({"groups": {"proxy": {}}}, {})
        refresh.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
