import io
import json
from pathlib import Path
import runpy
from subprocess import CompletedProcess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch


class HealthProbeTest(unittest.TestCase):
    endpoint = "https://127.0.0.1:8006/anas/installed"

    def setUp(self):
        self.manager = runpy.run_path(str(Path(__file__).with_name("anasctl")))

    def probe(self):
        return self.manager["local_http"](self.endpoint, "--insecure")

    def test_transient_http_failures_report_recovery(self):
        failure = CompletedProcess([], 22, "", "curl: (22) The requested URL returned error: 500\n")
        success = CompletedProcess([], 0, '{"name":"anas"}', "")
        console = io.StringIO()
        with patch("subprocess.run", side_effect=[failure, failure, success]) as run, patch("time.sleep"), redirect_stdout(console):
            self.manager["retry_health"](self.probe)
        self.assertEqual(console.getvalue(), "Health checks passed after 3 attempts.\n")
        self.assertTrue(all(call.kwargs["capture_output"] for call in run.call_args_list))

    def test_persistent_failure_identifies_endpoint_and_http_status(self):
        failure = CompletedProcess([], 22, "", "curl: (22) The requested URL returned error: 500\n")
        with patch("subprocess.run", return_value=failure) as run, patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, r"failed after 10 attempts.*?/anas/installed.*error: 500"):
                self.manager["retry_health"](self.probe)
        self.assertEqual(run.call_count, 10)

    def test_proxy_uses_the_node_configured_ipv6_address(self):
        namespace = self.manager["proxy_health"].__globals__
        probe = Mock(return_value="Proxmox Virtual Environment")
        with patch.dict(namespace, {"output": lambda *args: "2001:db8::25", "active": lambda unit: True, "local_http": probe}):
            self.assertEqual(self.manager["proxy_health"](), "https://[2001:db8::25]:8006")
        probe.assert_called_once_with("https://[2001:db8::25]:8006/", "--insecure")

    def test_proxy_tries_ipv6_loopback_if_ipv4_is_unavailable(self):
        namespace = self.manager["proxy_health"].__globals__
        probe = Mock(side_effect=[RuntimeError("Connection refused"), "Proxmox Virtual Environment"])
        with patch.dict(namespace, {"output": lambda *args: "", "active": lambda unit: True, "local_http": probe}):
            self.assertEqual(self.manager["proxy_health"](), "https://[::1]:8006")
        self.assertEqual(probe.call_count, 2)


class NotificationTest(unittest.TestCase):
    def setUp(self):
        code = runpy.run_path(str(Path(__file__).with_name("anasctl")))
        self.manager = code["check"].__globals__

    def route(self, expression, target=None):
        target = target or {"name": "ops-alerts", "type": "webhook"}
        config = {
            "hostname": "pve-lab-02",
            "targets": [target],
            "matchers": [{"name": "storage-updates", "target": [target["name"]], "expression": json.dumps(expression)}],
        }
        return self.manager["notification_route"](config)

    def test_dedicated_matcher_works_with_an_arbitrary_target(self):
        expression = {"all-of": [
            {"match": {"type": "field", "field": "type", "values": ["anas-update-available"]}},
            {"match": {"type": "field", "field": "hostname", "values": ["pve-lab-02"]}},
            {"match": {"type": "severity", "severities": ["info"]}},
        ]}
        ready, message = self.route(expression)
        self.assertTrue(ready)
        self.assertIn("ops-alerts", message)

    def test_disabled_target_and_incompatible_severity_are_reported(self):
        ready, message = self.route({"constant": True}, {"name": "disabled", "type": "gotify", "disable": True})
        self.assertFalse(ready)
        self.assertIn("targets are disabled", message)
        ready, message = self.route({"match": {"type": "severity", "severities": ["error"]}})
        self.assertFalse(ready)
        self.assertIn("No enabled matcher", message)

    def test_mail_target_requires_a_recipient(self):
        target = {"name": "local-mail", "type": "sendmail", "has_recipient": False}
        ready, message = self.route({"constant": True}, target)
        self.assertFalse(ready)
        self.assertIn("no recipient", message)

    def test_native_calendar_and_regex_routing_remain_eligible(self):
        expression = {"any-of": [
            {"match": {"type": "calendar", "schedule": "mon..fri 07:00-08:00"}},
            {"not": {"match": {"type": "field", "field": "hostname", "regex": "^pve-"}}},
        ]}
        ready, message = self.route(expression)
        self.assertIsNone(ready)
        self.assertIn("Proxmox will evaluate", message)

    def test_inactive_worker_is_advisory(self):
        console = io.StringIO()
        with patch.dict(self.manager, {"notification_config": lambda: {"queued": True}, "active": lambda unit: False}), redirect_stderr(console):
            self.assertFalse(self.manager["notification_diagnostics"]())
        self.assertIn("notification worker is inactive", console.getvalue())
        self.assertIn("Daily release checks still run", console.getvalue())

    def test_release_is_sent_after_missing_route_is_fixed(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            state = Path(directory)
            version = state / "VERSION"
            version.write_text("0.4.5\n")
            send = Mock()
            route = Mock(side_effect=[False, True, True, True])
            with patch.dict(self.manager, {
                "STATE": state, "VERSION_FILE": version,
                "notification_diagnostics": route, "enqueue_update": send,
                "release_info": lambda: ("0.4.6", []), "release_asset": Mock(),
            }):
                self.manager["check"](True)
                self.assertFalse((state / "last-notified").exists())
                self.manager["check"](True)
                self.manager["check"](True)
                send.assert_called_once_with("0.4.5", "0.4.6")
                self.manager["check"](True, resend=True)
                self.assertEqual(send.call_count, 2)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.state = Path(self.scratch.name)
        code = runpy.run_path(str(Path(__file__).with_name("anasctl")))
        self.manager = code["install"].__globals__
        self.version = self.state / "VERSION"
        self.marker = self.state / "timer"
        self.packages = {"kept"}
        self.planned = {"anas-eval-deps": "0.4.6", "nodejs": "20"}
        self.busy = False
        self.fail_monitor = False
        self.fail_uninstaller = False
        self.fail_cleanup = False
        self.fail_restore = False
        source = Path(__file__).with_name("anasctl")
        overrides = {
            "STATE": self.state,
            "VERSION_FILE": self.version,
            "app_traces": lambda: [str(self.version)] if self.version.exists() else [],
            "schedule_units": lambda: [],
            "package_health": Mock(),
            "proxy_health": Mock(),
            "platform_preflight": Mock(),
            "notification_diagnostics": Mock(),
            "active": Mock(return_value=True),
            "installed_packages": lambda canonical: self.packages.copy(),
            "payload_files": lambda: {self.marker: source},
            "fetch": Mock(),
            "build_metapackage": lambda directory: directory / "fixture.deb",
            "apt_install_plan": lambda deb: ("", self.planned),
            "apply_apt": self.apt,
            "feature_state": lambda: {},
            "protected_packages": lambda scope: [],
            "set_package_mark": Mock(),
            "package_versions": lambda: {name: self.planned.get(name, "1") for name in self.packages},
            "cache_removals": lambda scope, backup, targets: {name: {"version": self.planned[name]} for name in targets},
            "restore_packages": self.restore_packages,
            "app_script": self.app,
            "remove_app": lambda scope: self.app(scope, "uninstall.sh"),
            "retry_health": lambda check: check(),
            "verify_app_removed": self.verify_removed,
            "app_health": lambda version: self.assertEqual(self.version.read_text(), version),
            "start_monitor": self.start_monitor,
            "stop_monitor": self.stop_monitor,
            "delete_monitor": lambda manifest: self.stop_monitor(),
            "monitor_health": lambda: self.assertTrue(self.marker.exists()),
            "prepare_uninstall": self.backup,
            "restore_uninstall": self.restore,
            "ensure_idle": self.ensure_idle,
            "dependency_cleanup": lambda scope: sorted(set(self.planned) & self.packages),
        }
        patcher = patch.dict(self.manager, overrides)
        patcher.start()
        self.addCleanup(patcher.stop)
        for redirect in (redirect_stdout, redirect_stderr):
            context = redirect(io.StringIO())
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def apt(self, scope, mode, packages, *args):
        if mode == "install":
            self.packages.update(packages)
            self.manager["write_json"](scope / "apt-install-fixture.json", sorted(packages))
        else:
            if self.fail_cleanup:
                self.packages.discard("nodejs")
                raise RuntimeError("APT remove failed")
            self.packages.difference_update(packages)

    def app(self, scope, script):
        if script == "install.sh":
            self.version.write_text("0.4.6")
        else:
            self.version.unlink()
            if self.fail_uninstaller:
                raise RuntimeError("upstream uninstall failed")

    def start_monitor(self, scope, manifest):
        self.marker.touch()
        if self.fail_monitor:
            raise RuntimeError("timer failed after activation")

    def stop_monitor(self):
        self.marker.unlink(missing_ok=True)

    def verify_removed(self):
        self.assertFalse(self.version.exists())

    def backup(self, scope):
        backup = scope / "uninstall-backup"
        backup.mkdir(exist_ok=True)
        return backup

    def ensure_idle(self):
        if self.busy:
            raise RuntimeError("ANAS has running work")

    def restore(self, scope, backup, manifest, app_changed):
        if app_changed:
            self.app(scope, "install.sh")
        self.start_monitor(scope, manifest)

    def restore_packages(self, scope, packages):
        if self.fail_restore:
            raise RuntimeError("APT restore failed")
        self.packages.update(packages)

    def call(self, operation):
        if operation == "install":
            self.manager[operation](False)
        else:
            self.manager[operation]()

    def assert_clean(self):
        self.assertFalse(self.version.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.packages, {"kept"})
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertFalse((self.state / "installed.json").exists())

    def assert_installed(self):
        self.assertTrue(self.version.exists())
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.packages, {"kept", *self.planned})
        self.assertTrue((self.state / "installed.json").exists())
        self.assertFalse((self.state / "transaction.json").exists())

    def test_install_uninstall_are_repeatable(self):
        self.call("install")
        self.call("install")
        self.assert_installed()
        self.call("uninstall")
        self.call("uninstall")
        self.assert_clean()

    def test_install_commits_when_notification_routing_is_unavailable(self):
        with patch.dict(self.manager, {"notification_diagnostics": Mock(return_value=False)}):
            self.call("install")
        self.assert_installed()

    def test_install_rolls_back_after_timer_failure(self):
        self.fail_monitor = True
        with self.assertRaisesRegex(RuntimeError, "timer failed"):
            self.call("install")
        self.assert_clean()

    def test_busy_uninstall_restarts_monitor_without_reinstalling_app(self):
        self.call("install")
        self.busy = True
        with patch.dict(self.manager, {"app_script": Mock(side_effect=AssertionError("app was changed"))}):
            with self.assertRaisesRegex(RuntimeError, "running work"):
                self.call("uninstall")
        self.assert_installed()

    def test_failed_upstream_removal_restores_app_and_monitor(self):
        self.call("install")
        self.fail_uninstaller = True
        with self.assertRaisesRegex(RuntimeError, "upstream uninstall failed"):
            self.call("uninstall")
        self.assert_installed()

    def test_partial_package_removal_restores_packages_app_and_timer(self):
        self.call("install")
        self.fail_cleanup = True
        with self.assertRaisesRegex(RuntimeError, "APT remove failed"):
            self.call("uninstall")
        self.assert_installed()

    def test_rollback_can_resume_after_interruption(self):
        self.call("install")
        self.fail_cleanup = True
        self.fail_restore = True
        with self.assertRaisesRegex(RuntimeError, "APT remove failed"):
            self.call("uninstall")
        self.assertFalse(self.version.exists())
        self.assertEqual(self.manager["read_json"](self.state / "transaction.json")["phase"], "remove-packages")
        self.fail_restore = False
        self.call("recover")
        self.assert_installed()


if __name__ == "__main__":
    unittest.main()
