"""Best-effort, at-most-one-attempt Feishu custom-bot alerts per switch event."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _log(**fields):
    try:
        print(json.dumps({"event": "feishu_notification", **fields}), flush=True)
    except Exception:
        pass


def _read_private(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Notification files must not be symlinks")
    if os.name == "posix":
        metadata = path.stat()
        if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
            raise PermissionError("Notification files must be private and owned by the current user")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".notification-", delete=False, encoding="utf-8") as stream:
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


def validate_config(options):
    """Read/validate private configuration without sending any network request."""
    credentials = _read_private(options["config_file"])
    url = urllib.parse.urlsplit(credentials["webhook"])
    if (url.scheme != "https" or url.hostname not in ("open.feishu.cn", "open.larksuite.com")
            or url.port not in (None, 443) or url.username or url.password or url.query or url.fragment
            or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", url.path)
            or url.path.endswith("/REPLACE_ME")):
        raise ValueError("Use an official HTTPS custom-bot v2 webhook")
    if not isinstance(credentials.get("secret", ""), str):
        raise ValueError("Signing secret must be text")
    if options.get("language", "zh") not in ("zh", "en"):
        raise ValueError("Notification language must be zh or en")
    timeout = options.get("timeout_seconds", 5)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 15:
        raise ValueError("Notification timeout must be between 1 and 15 seconds")
    if not isinstance(options.get("instance_name", "v2rayA"), str) or len(options.get("instance_name", "v2rayA")) > 120:
        raise ValueError("Instance name must be at most 120 characters")
    return credentials


def sign(timestamp, secret):
    key = (str(timestamp) + "\n" + secret).encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


def _node(which):
    return {key: which.get(key) for key in ("_type", "sub", "id")}


def _message(options, outbound, old, new, event_id):
    old_label = json.dumps(_node(old), ensure_ascii=False, separators=(",", ":"))
    new_label = json.dumps(_node(new), ensure_ascii=False, separators=(",", ":"))
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
    instance = options.get("instance_name", "v2rayA")
    if options.get("language", "zh") == "en":
        return f"v2rayA failover verified\nInstance: {instance}\nOutbound: {outbound}\nPrevious: {old_label}\nCurrent: {new_label}\nTime: {timestamp}\nEvent: {event_id}"
    return f"v2rayA 节点切换成功（连通性验证通过）\n实例：{instance}\n出站：{outbound}\n原节点：{old_label}\n新节点：{new_label}\n时间：{timestamp}\n事件：{event_id}"


def notify_switch(config, outbound, old, new, state):
    """Never propagate notification failures into proxy switching/rollback logic.

    Journal before POST. Never retry pending, uncertain, rejected or sent events;
    inspect the group before manually reconciling an ambiguous delivery.
    """
    receipt = None
    path = None
    attempted = False
    try:
        options = config.get("feishu", {})
        if options.get("enabled", False) is not True:
            return
        identity = {"outbound": outbound, "previous": _node(old), "current": _node(new), "switched_at": state["last_switch"]}
        event_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
        path = Path(config["state_file"]).parent / "notifications" / (event_id + ".json")
        if path.exists():
            previous = _read_private(path)
            if previous.get("event_id") == event_id:
                state["notification"] = {"event_id": event_id, "status": previous["status"]}
                return
        credentials = validate_config(options)
        payload = {"msg_type": "text", "content": {"text": _message(options, outbound, old, new, event_id)}}
        if credentials.get("secret"):
            timestamp = str(int(time.time()))
            payload.update(timestamp=timestamp, sign=sign(timestamp, credentials["secret"]))
        receipt = {**identity, "event_id": event_id, "status": "pending", "attempted_at": time.time()}
        _write_private(path, receipt)
        request = urllib.request.Request(credentials["webhook"], data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        # Independent direct connection; do not inherit a broken system proxy or
        # follow redirects that could disclose the webhook/signature payload.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        attempted = True
        with opener.open(request, timeout=options.get("timeout_seconds", 5)) as response:
            result = json.loads(response.read(65537))
        codes = [result[key] for key in ("code", "StatusCode") if key in result]
        if codes and all(type(code) is int and code == 0 for code in codes):
            receipt["status"] = "sent"
        else:
            receipt["status"] = "rejected" if codes else "uncertain"
            receipt["business_codes"] = [code for code in codes if type(code) is int]
        _write_private(path, receipt)
        state["notification"] = {"event_id": event_id, "status": receipt["status"]}
        _log(event_id=event_id, status=receipt["status"])
    except Exception as error:
        status = "uncertain" if attempted else "configuration_error"
        if receipt is not None:
            receipt["status"] = status
            try:
                _write_private(path, receipt)
            except Exception:
                pass
        try:
            state["notification"] = {"event_id": receipt.get("event_id") if receipt else None, "status": status}
        except Exception:
            pass
        _log(status=status, kind=type(error).__name__)
