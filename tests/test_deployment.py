import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/v2raya-failover/scripts"))
import manage
import v2raya_failover as watchdog


class AuthenticationTests(unittest.TestCase):
    def test_official_login_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-auth.json"
            watchdog.write_private(path, {"username": "test-operator", "password": "test-password"})
            config = {"api_url": "http://127.0.0.1:2017", "auth_file": str(path), "data_dir": directory}
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps({"code": "SUCCESS", "data": {"token": "test-official-token"}}).encode())
            with patch.object(watchdog, "read_bolt", return_value={("touch", "subscriptions"): b"[]"}), patch.object(watchdog.urllib.request, "build_opener", return_value=opener):
                api = watchdog.LocalAPI(config)
            self.assertEqual(api.token, "test-official-token")
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:2017/api/login")
            self.assertEqual(json.loads(request.data), {"username": "test-operator", "password": "test-password"})
            self.assertIsNone(request.get_header("Authorization"))

    def test_remote_management_endpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            watchdog.LocalAPI({"api_url": "http://192.0.2.10:2017"})

    @unittest.skipUnless(os.name == "posix", "POSIX mode check")
    def test_world_readable_auth_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                watchdog.read_private_json(path)

    def test_country_constraint_is_configurable(self):
        with patch.object(watchdog, "curl", return_value=(0, "200", "loc=JP\n", 1)):
            self.assertTrue(watchdog.healthy("http://127.0.0.1:1234", {"probes": [], "require_country": "JP"})[0])
            self.assertFalse(watchdog.healthy("http://127.0.0.1:1234", {"probes": [], "require_country": "US"})[0])


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / "operator-auth.json"
        watchdog.write_private(self.auth, {"token": "test-token"})
        self.config = json.loads((manage.ASSETS / "config.example.json").read_text())
        self.config["auth_file"] = str(self.auth)
        node = {"protocol": "vless", "net": "tcp", "tls": "reality", "add": "node.invalid", "port": 443, "id": "test-id", "pbk": "test-public-key", "sni": "example.org"}
        self.api = Mock()
        self.api.subs = [{"address": "https://subscription.invalid/private", "servers": [{"serverObj": {**node, "ps": name}} for name in ["US 1", "US 2", "HK 1", "HK 2"]]}]
        self.original_route = "default: proxy\ndomain(geosite:cn)->direct\n"
        self.routing = self.original_route
        self.setting = {"pacMode": "routingA", "proxyModeWhenSubscribe": "proxy", "subscriptionAutoUpdateMode": "none", "muxOn": "no"}
        self.writes = []

        def call(path, method="GET", value=None):
            if method != "GET":
                self.writes.append((path, method, copy.deepcopy(value)))
            if path == "outbounds":
                return {"outbounds": ["proxy", "downloads"]}
            if path == "touch":
                return {"running": True, "touch": {"connectedServer": [{"outbound": "proxy", "_type": "subscriptionServer", "sub": 0, "id": 1}, {"outbound": "downloads", "_type": "subscriptionServer", "sub": 0, "id": 3}]}}
            if path == "setting":
                if method == "PUT":
                    self.setting = copy.deepcopy(value)
                return {"setting": copy.deepcopy(self.setting)}
            if path == "routingA":
                if method == "PUT":
                    self.routing = value["routingA"]
                return {"routingA": self.routing}
            if path == "outbound?outbound=proxy":
                return {"setting": {"type": "leastping"}}

        self.api.call.side_effect = call

    def test_routing_preserves_unrelated_rules_and_is_idempotent(self):
        first = manage.planned_routing(self.config, self.api, self.original_route)
        second = manage.planned_routing(self.config, self.api, first)
        self.assertEqual(first, second)
        self.assertEqual(manage.strip_managed(first), self.original_route)
        self.assertIn("domain(full: subscription.invalid)->downloads", first)
        self.assertIn("domain(domain: docker.io)->downloads", first)

    def test_ambiguous_managed_blocks_are_rejected(self):
        with self.assertRaises(manage.ConfigurationError):
            manage.strip_managed(manage.BEGIN + "\n" + manage.BEGIN + "\n" + manage.END)

    def test_conflicting_probe_and_business_routes_are_rejected(self):
        self.config["groups"]["downloads"]["routing_domains"].append("openai.com")
        with self.assertRaises(manage.ConfigurationError):
            manage.planned_routing(self.config, self.api, self.original_route)

    @patch.object(manage, "require_root")
    @patch.object(watchdog, "LocalAPI")
    @patch.object(manage.subprocess, "check_output", side_effect=["2.2.7.4\n", "Xray 25.10.15 test\n"])
    def test_dry_run_has_no_file_or_api_mutations(self, _, api_class, root):
        api_class.return_value = self.api
        before = sorted(str(p) for p in self.root.rglob("*"))
        manage.install(self.config, self.root, no_systemd=True, dry_run=True)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        self.assertEqual(self.writes, [])

    def test_automatic_subscription_interval_is_rejected(self):
        self.config["subscription_refresh_seconds"] = 21600
        with self.assertRaises(manage.ConfigurationError):
            manage.validate(self.config, self.api)

    @patch.object(manage, "require_root")
    @patch.object(watchdog, "LocalAPI")
    @patch.object(manage.subprocess, "check_output", side_effect=["2.2.7.4\n", "Xray 25.10.15 test\n", "2.2.7.4\n", "Xray 25.10.15 test\n"])
    @patch.object(watchdog, "backup", return_value="private-backup")
    @patch.object(watchdog, "run", return_value=True)
    def test_private_notification_copy_and_no_secret_in_preview(self, run, backup, version, api_class, root):
        api_class.return_value = self.api
        credentials = self.root / "operator-feishu.json"
        webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-not-a-real-token"
        watchdog.write_private(credentials, {"webhook": webhook, "secret": "unit-test-secret"})
        self.config["feishu"].update(enabled=True, config_file=str(credentials))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            manage.install(self.config, self.root, no_systemd=True, dry_run=True)
        self.assertNotIn(webhook, output.getvalue())
        self.assertNotIn("unit-test-secret", output.getvalue())
        manage.install(self.config, self.root, no_systemd=True)
        paths = manage.layout(self.root)
        installed = watchdog.read_private_json(paths["config"])
        self.assertEqual(installed["feishu"]["config_file"], str(paths["feishu"]))
        self.assertNotIn("webhook", json.dumps(installed))
        self.assertEqual(watchdog.read_private_json(paths["feishu"])["webhook"], webhook)
        self.assertTrue(paths["notifier"].exists())

    @patch.object(manage, "require_root")
    @patch.object(watchdog, "LocalAPI")
    @patch.object(manage.subprocess, "check_output", side_effect=["2.2.7.4\n", "Xray 25.10.15 test\n"])
    @patch.object(watchdog, "backup", return_value="private-backup")
    @patch.object(watchdog, "run", return_value=False)
    def test_unhealthy_install_rolls_back_routes_and_settings(self, _, backup, version, api_class, root):
        api_class.return_value = self.api
        original_setting = copy.deepcopy(self.setting)
        with self.assertRaises(manage.ConfigurationError):
            manage.install(self.config, self.root, no_systemd=True)
        self.assertEqual(self.routing, self.original_route)
        self.assertEqual(self.setting, original_setting)
        self.assertEqual(watchdog.read_private_json(manage.layout(self.root)["manifest"])["status"], "rolled_back")

    def test_removal_preserves_operator_setting_changes(self):
        self.routing = manage.planned_routing(self.config, self.api, self.original_route) + "domain(full: operator.invalid)->direct\n"
        self.setting.update(proxyModeWhenSubscribe="pac", muxOn="yes")
        manage.restore_native(self.api, {"previous_setting": {"proxyModeWhenSubscribe": "proxy", "subscriptionAutoUpdateMode": "none"}, "applied_setting": {"proxyModeWhenSubscribe": "pac", "subscriptionAutoUpdateMode": "none"}})
        self.assertEqual(self.setting["muxOn"], "yes")
        self.assertEqual(self.setting["proxyModeWhenSubscribe"], "proxy")
        self.assertIn("operator.invalid", self.routing)
        self.assertNotIn(manage.BEGIN, self.routing)


if __name__ == "__main__":
    unittest.main()
