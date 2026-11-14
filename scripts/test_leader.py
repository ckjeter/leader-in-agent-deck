import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("leader", Path(__file__).with_name("leader.py"))
leader = importlib.util.module_from_spec(spec)
was_disabled = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    spec.loader.exec_module(leader)
finally:
    sys.dont_write_bytecode = was_disabled


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.brief = self.root / "task.md"
        self.brief.write_text("Implement the task.")
        self.args = argparse.Namespace(worker="test", path=str(self.root), brief=str(self.brief),
                                       title="Task", direct=False, worktree=None, cls="impl")
        self.roster = {"workers": {"test": {"pool": "go", "launch": ["-c", "pi"], "direct": ["pi", "-p"]}}}
        self.patches = [patch.object(leader, "HOME", self.root / "state"),
                        patch.object(leader, "load_roster", return_value=self.roster),
                        patch.object(leader.shutil, "which", return_value="/fake/pi"),
                        patch.object(leader.subprocess, "Popen")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.popen = leader.subprocess.Popen
        self.popen.return_value.pid = 123

    def launch(self, sessions, response, ok=False):
        with patch.object(leader, "ad_json", side_effect=sessions), \
             patch.object(leader, "ad", return_value=(ok, response)) as ad, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                leader.cmd_launch(self.args)
            except SystemExit as e:
                self.assertEqual(e.code, 1)
            return json.loads(output.getvalue()), ad

    def test_success_stays_in_agent_deck_and_keeps_flags_before_path(self):
        result, ad = self.launch([[]], '{"success":true,"session_id":"new"}', ok=True)
        self.assertEqual(result["session"], "new")
        self.assertEqual(result["via"], "agent-deck")
        self.assertEqual(ad.call_args.args[-1], str(self.root))
        self.assertIn("--json", ad.call_args.args[:-1])
        self.assertIn("-inherit-group", ad.call_args.args[:-1])
        self.assertIn("===AGENTDECK_DONE===", (Path(result["result"]).parent / "brief.md").read_text())
        self.popen.assert_not_called()

    def test_partial_launch_is_recorded_without_restart_resend_or_duplicate(self):
        child = {"id": "new", "title": "Task [test]", "status": "error"}
        result, ad = self.launch([[], [child]], "startup timed out")
        self.assertEqual(result["session"], "new")
        self.assertEqual(result["launch_error"], "startup timed out")
        self.assertEqual(ad.call_count, 1)
        self.assertEqual(ad.call_args.args[0], "launch")
        self.assertEqual(json.loads((Path(result["result"]).parent / "meta.json").read_text())["session"], "new")
        self.popen.assert_not_called()

    def test_does_not_adopt_an_old_session_with_the_same_title(self):
        old = {"id": "old", "title": "Task [test]"}
        result, _ = self.launch([[old], [old]], "launch failed")
        self.assertIsNone(result["session"])
        self.popen.assert_not_called()

    def test_ambiguous_new_sessions_are_not_restarted(self):
        sessions = [{"id": sid, "title": "Task [test]"} for sid in ("one", "two")]
        result, ad = self.launch([[], sessions], "launch failed")
        self.assertIsNone(result["session"])
        self.assertEqual(ad.call_count, 1)
        self.popen.assert_not_called()

    def test_unknown_launch_result_never_falls_back(self):
        result, _ = self.launch([[], None], "timed out")
        self.assertIn("error", result)
        self.popen.assert_not_called()

    def test_empty_launch_failure_is_recorded(self):
        result, _ = self.launch([[], []], "")
        self.assertEqual(result["launch_error"], "agent-deck launch returned no result")
        self.popen.assert_not_called()

    def test_direct_fallback_only_when_preflight_is_down(self):
        result, ad = self.launch([None], "unused")
        self.assertEqual(result["via"], "direct")
        ad.assert_not_called()
        self.popen.assert_called_once()
        self.assertIn("===AGENTDECK_DONE===", self.popen.call_args.args[0][-1])

    def test_direct_fallback_does_not_ignore_worktree_isolation(self):
        self.args.worktree = "task-branch"
        result, _ = self.launch([None], "unused")
        self.assertIn("worktree", result["error"])
        self.popen.assert_not_called()

    def test_wait_reports_launch_error_without_sending(self):
        result, _ = self.launch([[], []], "startup timed out")
        with patch.object(leader, "ad") as ad, contextlib.redirect_stdout(io.StringIO()) as output:
            leader.cmd_wait(argparse.Namespace(run=result["run"], timeout=1, interval=0))
        self.assertEqual(json.loads(output.getvalue())["state"], "error")
        ad.assert_not_called()

    def test_wait_reports_a_live_direct_process_limit(self):
        result, _ = self.launch([None], "unused")
        (Path(result["result"]).parent / "stdout.log").write_text("429 GoUsageLimitError: OpenCode Go usage limit reached")
        with patch.object(leader, "pid_alive", return_value=True), contextlib.redirect_stdout(io.StringIO()) as output:
            leader.cmd_wait(argparse.Namespace(run=result["run"], timeout=1, interval=0))
        self.assertEqual(json.loads(output.getvalue())["state"], "limit")
        events = leader.read_ledger(leader.host(self.roster))
        self.assertEqual([(e["type"], e["pool"]) for e in events], [("pool_limit", "go")])

    def roster_report(self, cls="impl"):
        with patch.object(leader, "host", return_value="test"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            leader.cmd_roster(argparse.Namespace(cls=cls))
        return json.loads(output.getvalue())

    def two_pools(self, used_a, used_b):
        soon = leader.time.time() + 3600
        self.roster.update({
            "pools": {"a": {"gauge": "agent-deck"}, "b": {"gauge": "agent-deck"}, "go": {}},
            "defaults": {"impl": ["first", "second"]},
            "workers": {"first": {"pool": "a", "launch": [], "direct": ["pi"]},
                        "second": {"pool": "b", "launch": [], "direct": ["pi"]},
                        "toy": {"pool": "go", "launch": [], "direct": ["pi"]}}})
        usage = {"providers": [{"id": pool, "updated_at": int(leader.time.time()),
                                "windows": [{"label": "5h", "used_percentage": used, "resets_at": soon}]}
                               for pool, used in (("a", used_a), ("b", used_b))]}
        return patch.object(leader, "ad_json", return_value=usage)

    def test_route_skips_a_pool_over_its_cap(self):
        with self.two_pools(85, 10):
            report = self.roster_report()
        self.assertFalse(report["pools"]["a"]["ok"])
        self.assertEqual(report["route"]["impl"], {"pick": "second", "bakeoff_challenger": None})

    def test_bakeoff_needs_room_in_both_pools_and_never_uses_a_pool_outside_defaults(self):
        with self.two_pools(10, 10):
            self.assertEqual(self.roster_report()["route"]["impl"], {"pick": "first", "bakeoff_challenger": "second"})
        with self.two_pools(10, 70):
            self.assertEqual(self.roster_report()["route"]["impl"], {"pick": "first", "bakeoff_challenger": None})
        with self.two_pools(70, 10):
            self.assertEqual(self.roster_report()["route"]["impl"], {"pick": "first", "bakeoff_challenger": None})

    def test_bakeoff_can_be_switched_off_in_the_roster(self):
        with self.two_pools(10, 10):
            self.roster["bakeoff"] = {"enabled": False}
            self.assertIsNone(self.roster_report()["route"]["impl"]["bakeoff_challenger"])

    def test_a_recent_limit_closes_the_pool_and_an_old_one_does_not(self):
        for age, ok in ((60, False), (6 * 3600, True)):
            with self.subTest(age=age), self.two_pools(10, 10):
                ts = leader.datetime.fromtimestamp(leader.time.time() - age).astimezone().isoformat(timespec="seconds")
                leader.ledger_file("test").unlink(missing_ok=True)
                leader.append_event("test", {"type": "pool_limit", "ts": ts, "pool": "a"})
                self.assertEqual(self.roster_report()["pools"]["a"]["ok"], ok)

    def test_a_window_that_reset_after_the_reading_does_not_block(self):
        with self.two_pools(95, 10) as usage:
            usage.return_value["providers"][0]["windows"][0]["resets_at"] = leader.time.time() - 60
            self.assertTrue(self.roster_report()["pools"]["a"]["ok"])

    def test_codex_limits_are_read_from_the_newest_session_file(self):
        day = self.root / "codex" / "sessions" / "2026" / "01" / "01"
        day.mkdir(parents=True)
        line = {"payload": {"rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 9},
                                            "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": 9}}}}
        (day / "rollout-a.jsonl").write_text("{}\n" + json.dumps(line) + "\n{}\n")
        with patch.dict(leader.os.environ, {"CODEX_HOME": str(self.root / "codex")}):
            usage = leader.codex_usage()
        self.assertEqual([(w["label"], w["used_percentage"]) for w in usage["windows"]], [("5h", 12.0), ("7d", 40.0)])

    def wait(self, status, output):
        sessions = [{"id": "new", "status": status}]
        with patch.object(leader, "ad_json", return_value=sessions), \
             patch.object(leader, "ad", return_value=(True, output)), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            leader.cmd_wait(argparse.Namespace(run=self.run, timeout=0.05, interval=0))
        return json.loads(out.getvalue())["state"]

    def test_wait_after_send_ignores_the_previous_round(self):
        result, _ = self.launch([[]], '{"success":true,"session_id":"new"}', ok=True)
        self.run = result["run"]
        old = "done\n===AGENTDECK_DONE=== status=ok summary=first round"
        with patch.object(leader, "ad", return_value=(True, old)) as ad, contextlib.redirect_stdout(io.StringIO()):
            leader.cmd_send(argparse.Namespace(run=self.run, message="fix the off-by-one", message_file=None))
        self.assertEqual(ad.call_args.args[:3], ("session", "send", "--message-file"))
        meta = json.loads((Path(result["result"]).parent / "meta.json").read_text())
        self.assertEqual(meta["followups"], 1)
        self.assertEqual(self.wait("waiting", old), "timeout")
        self.assertEqual(self.wait("waiting", "fixed\n===AGENTDECK_DONE=== status=ok summary=second"), "done")

    def test_send_refuses_a_direct_worker(self):
        result, _ = self.launch([None], "unused")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            leader.cmd_send(argparse.Namespace(run=result["run"], message="x", message_file=None))

    def test_roster_marks_workers_whose_cli_is_missing(self):
        self.roster["workers"]["gone"] = {"launch": ["-c", "codex"], "direct": ["codex", "exec"]}
        with patch.object(leader.shutil, "which", side_effect=lambda exe: "/fake/pi" if exe == "pi" else None), \
             patch.object(leader, "host", return_value="test"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            leader.cmd_roster(argparse.Namespace(cls="impl"))
        workers = json.loads(output.getvalue())["workers"]
        self.assertTrue(workers["test"]["available"])
        self.assertFalse(workers["gone"]["available"])

    def test_indented_done_and_need_lines_are_seen(self):
        # Codex indents its final message by two spaces in `agent-deck session output`.
        self.assertEqual(leader.classify("  work\n  ===AGENTDECK_DONE=== status=ok summary=reviewed"), ("done", "reviewed"))
        self.assertEqual(leader.classify("\t===AGENTDECK_DONE=== status=fail summary=blocked"), ("fail", "blocked"))
        self.assertEqual(leader.classify("  NEED: which branch?"), ("needs_input", "NEED: which branch?"))

    def test_an_echo_of_the_brief_is_not_a_result(self):
        echoed = leader.DIRECT_SENTINEL + leader.FOLLOWUP.format(result="/tmp/result.md")
        self.assertEqual(leader.classify(echoed), (None, None))

    def test_usage_limit_variants(self):
        for text in ("You've hit your 5-hour limit", "You've hit your usage limit",
                     "GoUsageLimitError", "quota exceeded"):
            with self.subTest(text=text):
                self.assertEqual(leader.classify(text)[0], "limit")


if __name__ == "__main__":
    unittest.main()
