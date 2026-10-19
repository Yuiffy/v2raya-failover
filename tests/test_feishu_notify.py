import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/v2raya-failover/scripts"))
import feishu_notify as notifier
import v2raya_failover as watchdog


class NotificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.credentials = self.root / "feishu.json"
        self.webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-not-a-real-token"
        notifier._write_private(self.credentials, {"webhook": self.webhook, "secret": "unit-test-secret"})
        self.config = {"state_file": str(self.root / "state.json"), "feishu": {"enabled": True, "config_file": str(self.credentials), "instance_name": "test instance", "language": "en"}}
        self.old = {"_type": "subscriptionServer", "sub": 0, "id": 1, "Link": "must-not-be-sent"}
        self.new = {"_type": "subscriptionServer", "sub": 0, "id": 2}
        self.state = {"last_switch": 1700000000}

    def deliver(self, response):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(response).encode())
        with patch.object(notifier.urllib.request, "build_opener", return_value=opener) as factory:
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
        return opener, factory

    def receipt(self):
        return notifier._read_private(self.root / "notifications" / (self.state["notification"]["event_id"] + ".json"))

    def test_disabled_does_not_read_secret_or_send(self):
        self.config["feishu"]["enabled"] = False
        with patch.object(notifier, "validate_config") as validate, patch.object(notifier.urllib.request, "build_opener") as factory:
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
        validate.assert_not_called()
        factory.assert_not_called()
        self.assertFalse((self.root / "notifications").exists())

    def test_truthy_string_is_not_opt_in(self):
        self.config["feishu"]["enabled"] = "false"
        with patch.object(notifier.urllib.request, "build_opener") as factory:
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
        factory.assert_not_called()

    def test_signature_matches_known_vector(self):
        self.assertEqual(notifier.sign(1700000000, "unit-test-secret"), "wjfRqjvrFW1O9p5NWa2DHJCyt/9bB36ZHkDxu0POpJw=")

    def test_success_signed_and_no_private_node_data(self):
        with patch.object(notifier.time, "time", return_value=1700000000):
            opener, factory = self.deliver({"code": 0, "msg": "success"})
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["timestamp"], "1700000000")
        self.assertEqual(payload["sign"], notifier.sign(1700000000, "unit-test-secret"))
        self.assertIn("failover verified", payload["content"]["text"])
        self.assertNotIn("must-not-be-sent", payload["content"]["text"])
        self.assertNotIn(self.webhook, payload["content"]["text"])
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], notifier.NoRedirect)
        self.assertEqual(self.receipt()["status"], "sent")

    def test_legacy_success_response(self):
        self.deliver({"StatusCode": 0, "StatusMessage": "success"})
        self.assertEqual(self.receipt()["status"], "sent")

    def test_unsigned_chinese_notification(self):
        notifier._write_private(self.credentials, {"webhook": self.webhook})
        self.config["feishu"]["language"] = "zh"
        opener, _ = self.deliver({"code": 0})
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("timestamp", payload)
        self.assertNotIn("sign", payload)
        self.assertIn("节点切换成功", payload["content"]["text"])

    def test_business_rejection_is_not_success(self):
        self.deliver({"code": 19024, "msg": "Key Words Not Found"})
        self.assertEqual(self.receipt()["status"], "rejected")

    def test_conflicting_business_codes_are_rejected(self):
        self.deliver({"code": 0, "StatusCode": 19024})
        self.assertEqual(self.receipt()["status"], "rejected")

    def test_missing_business_code_is_uncertain(self):
        self.deliver({"msg": "not a delivery receipt"})
        self.assertEqual(self.receipt()["status"], "uncertain")

    def test_timeout_has_one_attempt_no_retry_and_no_secret_log(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError(self.webhook + " unit-test-secret")
        output = io.StringIO()
        with patch.object(notifier.urllib.request, "build_opener", return_value=opener), contextlib.redirect_stdout(output):
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(self.receipt()["status"], "uncertain")
        self.assertNotIn(self.webhook, output.getvalue())
        self.assertNotIn("unit-test-secret", output.getvalue())

    def test_pending_journal_is_written_before_post(self):
        opener = Mock()

        def post(*args, **kwargs):
            journal = list((self.root / "notifications").glob("*.json"))
            self.assertEqual(len(journal), 1)
            self.assertEqual(notifier._read_private(journal[0])["status"], "pending")
            return io.BytesIO(b'{"code":0}')

        opener.open.side_effect = post
        with patch.object(notifier.urllib.request, "build_opener", return_value=opener):
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)

    def test_completed_event_is_not_sent_twice(self):
        self.deliver({"code": 0})
        with patch.object(notifier.urllib.request, "build_opener") as factory:
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
        factory.assert_not_called()

    def test_unsafe_webhook_is_rejected_without_network(self):
        for url in ["http://open.feishu.cn/open-apis/bot/v2/hook/test", "https://open.feishu.cn.evil.invalid/open-apis/bot/v2/hook/test", "https://open.feishu.cn/open-apis/bot/v2/hook/test?redirect=1"]:
            notifier._write_private(self.credentials, {"webhook": url})
            with self.assertRaises(ValueError):
                notifier.validate_config(self.config["feishu"])

    def test_configuration_error_does_not_escape(self):
        self.config["feishu"]["config_file"] = str(self.root / "missing.json")
        with patch.object(notifier.urllib.request, "build_opener") as factory:
            notifier.notify_switch(self.config, "proxy", self.old, self.new, self.state)
        factory.assert_not_called()
        self.assertEqual(self.state["notification"]["status"], "configuration_error")


class SwitchNotificationTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.current = {"_type": "subscriptionServer", "sub": 0, "id": 1, "outbound": "proxy"}
        self.which = {**self.current, "id": 2}
        self.node = {"protocol": "vless", "add": "node.invalid", "port": 443, "id": "unit-id"}
        self.api.config = {"proxy_url": "http://127.0.0.1:20172", "verify_ports": [20171, 20172]}
        self.api.subs = [{"servers": [{"serverObj": {}}, {"serverObj": self.node}]}]
        self.api.call.return_value = {"touch": {"connectedServer": [self.current]}}

    @patch.object(watchdog, "notify_switch")
    @patch.object(watchdog, "backup", return_value="private-backup")
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy")
    def test_alert_only_after_both_ports_pass_and_no_healthy_repeat(self, health, candidate, backup, notify):
        health.side_effect = [(False, 8), (False, 8), (True, 1), (True, 1), (True, 1)]
        candidate.return_value = self.which, self.node
        state = {"failures": 1}
        self.assertTrue(watchdog.guard_group(self.api, "proxy", {}, state))
        self.assertTrue(watchdog.guard_group(self.api, "proxy", {}, state))
        notify.assert_called_once_with(self.api.config, "proxy", self.current, self.which, state)

    @patch.object(watchdog, "notify_switch", side_effect=RuntimeError("notifier failed"))
    @patch.object(watchdog, "backup", return_value="private-backup")
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy")
    def test_notifier_failure_never_rolls_back_working_node(self, health, candidate, backup, notify):
        health.side_effect = [(False, 8), (False, 8), (True, 1), (True, 1)]
        candidate.return_value = self.which, self.node
        self.assertTrue(watchdog.guard_group(self.api, "proxy", {}, {"failures": 1}))
        writes = [call.args for call in self.api.call.call_args_list if len(call.args) > 1]
        self.assertEqual(writes, [("connection", "POST", self.which)])

    @patch.object(watchdog, "notify_switch")
    @patch.object(watchdog, "backup", return_value="private-backup")
    @patch.object(watchdog, "best_candidate")
    @patch.object(watchdog, "healthy")
    def test_failed_switch_does_not_send_success_alert(self, health, candidate, backup, notify):
        health.side_effect = [(False, 8), (False, 8), (False, 8)]
        candidate.return_value = self.which, self.node
        with self.assertRaises(RuntimeError):
            watchdog.guard_group(self.api, "proxy", {}, {"failures": 1})
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
