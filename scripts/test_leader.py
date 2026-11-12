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
        self.roster = {"workers": {"test": {"launch": ["-c", "pi"], "direct": ["pi", "-p"]}}}
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
