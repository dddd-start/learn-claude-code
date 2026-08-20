"""Deterministic unit tests for s21_context_management (no API calls).

Reuses the fake-module load harness from test_compaction_tool_pairs.py:
stub `anthropic` + `dotenv` in sys.modules, chdir to a tempdir, set
MODEL_ID / ANTHROPIC_API_KEY, exec the module, then test pure functions.

Two injection seams make the tests API-free:
  - module.summarize_history  (monkeypatched, like the s08 tests)
  - Runner.delegate_fn        (fake item analyzer)
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
S21_PATH = REPO_ROOT / "s21_context_management" / "s21_code.py"


def load_module(name: str, path: Path, temp_cwd: Path):
    fake_anthropic = types.ModuleType("anthropic")

    class FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = types.SimpleNamespace(create=None)

    fake_dotenv = types.ModuleType("dotenv")
    setattr(fake_anthropic, "Anthropic", FakeAnthropic)
    setattr(fake_dotenv, "load_dotenv", lambda override=True: None)

    previous_anthropic = sys.modules.get("anthropic")
    previous_dotenv = sys.modules.get("dotenv")
    previous_cwd = Path.cwd()
    previous_model = os.environ.get("MODEL_ID")
    previous_key = os.environ.get("ANTHROPIC_API_KEY")

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)

    sys.modules["anthropic"] = fake_anthropic
    sys.modules["dotenv"] = fake_dotenv
    os.environ["MODEL_ID"] = "test-model"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        os.chdir(temp_cwd)
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)
        if previous_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = previous_anthropic
        if previous_dotenv is None:
            sys.modules.pop("dotenv", None)
        else:
            sys.modules["dotenv"] = previous_dotenv
        if previous_model is None:
            os.environ.pop("MODEL_ID", None)
        else:
            os.environ["MODEL_ID"] = previous_model
        if previous_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous_key


def _message_has_tool_use(message):
    content = message.get("content")
    return (
        message.get("role") == "assistant"
        and isinstance(content, list)
        and any(getattr(block, "type", None) == "tool_use" for block in content)
    )


def assert_no_orphan_tool_results(testcase, messages):
    for idx, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        if not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            continue
        testcase.assertGreater(idx, 0)
        testcase.assertTrue(_message_has_tool_use(messages[idx - 1]), messages)


class EstimateAndSnipTests(unittest.TestCase):
    def test_estimate_size_is_char_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_estimate_under_test", S21_PATH, Path(tmp))
            messages = [{"role": "user", "content": "abc"}, {"role": "assistant", "content": "de"}]
            self.assertEqual(module.estimate_size(messages), len(str(messages)))

    def test_snip_compact_limits_and_keeps_head_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_snip_under_test", S21_PATH, Path(tmp))
            messages = [{"role": "user", "content": "mission briefing"}]
            for i in range(30):
                messages.append({"role": "user", "content": f"[h{i}] node-{i} delegated"})
                messages.append({"role": "assistant", "content": f"[h{i}] healthy"})
            compacted = module.snip_compact(list(messages), max_messages=6)
            self.assertLessEqual(len(compacted), 7)  # head 3 + placeholder + tail 3
            self.assertEqual(compacted[0], messages[0])      # head preserved
            self.assertEqual(compacted[-1], messages[-1])    # tail preserved
            assert_no_orphan_tool_results(self, compacted)


class CompactLadderTests(unittest.TestCase):
    def test_compact_ladder_noop_when_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_noop_under_test", S21_PATH, Path(tmp))
            messages = [{"role": "user", "content": "x"}]
            before = list(messages)
            snips, comps = module.compact_ladder(messages, context_limit=10000, max_messages=100)
            self.assertEqual((snips, comps), (0, 0))
            self.assertEqual(messages, before)

    def test_compact_ladder_snips_when_many(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_snip_ladder_under_test", S21_PATH, Path(tmp))
            messages = [{"role": "user", "content": "mission"}]
            for i in range(20):
                messages.append({"role": "user", "content": f"[h{i}] x"})
                messages.append({"role": "assistant", "content": f"[h{i}] y"})
            snips, comps = module.compact_ladder(messages, context_limit=10**9, max_messages=10)
            self.assertEqual((snips, comps), (1, 0))
            self.assertLessEqual(len(messages), 11)  # head 3 + placeholder + tail 7

    def test_compact_ladder_llm_summary_when_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_compact_ladder_under_test", S21_PATH, Path(tmp))
            original = [{"role": "user", "content": "mission " * 500}]
            messages = list(original)
            captured = {}

            def fake_summarize(passed, _store=captured):
                _store["input"] = list(passed)
                return "the summary"

            module.summarize_history = fake_summarize
            snips, comps = module.compact_ladder(messages, context_limit=100, max_messages=1000)
            self.assertEqual((snips, comps), (0, 1))
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["role"], "user")
            self.assertIn("[Compacted]", messages[0]["content"])
            self.assertIn("the summary", messages[0]["content"])
            self.assertEqual(len(captured["input"]), len(original))
            self.assertEqual(captured["input"][0]["content"], original[0]["content"])


class SummarizeAndCheckpointTests(unittest.TestCase):
    def test_mock_summarize_deterministic_no_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_mocksum_under_test", S21_PATH, Path(tmp))
            messages = [{"role": "user", "content": "mission"}]
            for i in range(4):
                messages.append({"role": "user", "content": f"[h{i}] delegated"})
                messages.append({"role": "assistant", "content": f"[h{i}] healthy"})
            s1 = module.mock_summarize(list(messages))
            s2 = module.mock_summarize(list(messages))
            self.assertEqual(s1, s2)
            self.assertIn("hours logged so far: 4", s1)

    def test_checkpoint_round_trip_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_checkpoint_under_test", S21_PATH, Path(tmp))
            path = Path(tmp) / "checkpoint.json"
            cp = module.Checkpoint(mode="managed", next_index=7, limit=72,
                                   context_limit=1200, max_messages=24, compaction_count=2)
            cp.save(path)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())  # atomic: no .tmp leftover
            loaded = module.Checkpoint.from_dict(json.loads(path.read_text()), None)
            self.assertEqual(loaded.next_index, 7)
            self.assertEqual(loaded.compaction_count, 2)
            self.assertEqual(loaded.mode, "managed")

    def test_checkpoint_from_dict_fills_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_checkpoint_defaults_under_test", S21_PATH, Path(tmp))
            defaults = module.Checkpoint(limit=5, context_limit=999)
            loaded = module.Checkpoint.from_dict({"next_index": 3}, defaults)
            self.assertEqual(loaded.next_index, 3)
            self.assertEqual(loaded.limit, 5)
            self.assertEqual(loaded.context_limit, 999)
            self.assertEqual(loaded.max_messages, module.DEFAULT_MAX_MESSAGES)

    def test_load_or_init_defaults_on_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_loadinit_under_test", S21_PATH, Path(tmp))
            defaults = module.Checkpoint(limit=5)
            init = module.Checkpoint.load_or_init(Path(tmp) / "missing.json", defaults)
            self.assertEqual(init.limit, 5)
            self.assertEqual(init.next_index, 0)


class RunnerTests(unittest.TestCase):
    def test_resume_starts_at_next_index_and_no_double_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_resume_under_test", S21_PATH, Path(tmp))
            state_dir = Path(tmp) / "state"
            fake = lambda item: "healthy"
            r1 = module.Runner(mode="managed", state_dir=state_dir, limit=5,
                               crash_after=3, delegate_fn=fake)
            with self.assertRaises(SystemExit) as ctx:
                r1.run()
            self.assertEqual(ctx.exception.code, 3)
            self.assertEqual([r["index"] for r in module.read_work_log(state_dir / "work_log.jsonl")],
                             [0, 1, 2])
            cp = module.Checkpoint.load_or_init(state_dir / "checkpoint.json", None)
            self.assertEqual(cp.next_index, 3)
            # resume: continues at next_index, no double work
            r2 = module.Runner(mode="managed", state_dir=state_dir, limit=5, delegate_fn=fake)
            r2.run()
            self.assertEqual([r["index"] for r in module.read_work_log(state_dir / "work_log.jsonl")],
                             [0, 1, 2, 3, 4])

    def test_naive_overflow_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_naive_under_test", S21_PATH, Path(tmp))
            state_dir = Path(tmp) / "state"
            fake = lambda item: "healthy"
            r = module.Runner(mode="naive", state_dir=state_dir, limit=72,
                              context_limit=600, delegate_fn=fake)
            r.run()
            cp = module.Checkpoint.load_or_init(state_dir / "checkpoint.json", None)
            self.assertTrue(cp.overflowed)
            self.assertIsNotNone(cp.overflow_index)
            self.assertEqual(len(module.read_work_log(state_dir / "work_log.jsonl")),
                             cp.overflow_index)

    def test_managed_run_completes_all_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_managed_under_test", S21_PATH, Path(tmp))
            module.summarize_history = lambda msgs: "summary"   # no real API
            state_dir = Path(tmp) / "state"
            fake = lambda item: "healthy"
            r = module.Runner(mode="managed", state_dir=state_dir, limit=10,
                              context_limit=200, delegate_fn=fake)
            r.run()
            cp = module.Checkpoint.load_or_init(state_dir / "checkpoint.json", None)
            self.assertEqual(cp.next_index, 10)
            self.assertFalse(cp.overflowed)
            self.assertGreater(cp.compaction_count, 0)  # compaction path exercised
            self.assertEqual(len(module.read_work_log(state_dir / "work_log.jsonl")), 10)


class ReactiveAndItemTests(unittest.TestCase):
    def test_reactive_compact_keeps_tail_and_summarizes_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_reactive_under_test", S21_PATH, Path(tmp))
            module.write_transcript = lambda _m: Path("transcript.jsonl")
            captured = {}

            def fake_summarize(passed, _store=captured):
                _store["messages"] = list(passed)
                return "summary"

            module.summarize_history = fake_summarize
            messages = [{"role": "user", "content": "mission"}]
            for i in range(8):
                messages.append({"role": "user", "content": f"[h{i}] x"})
                messages.append({"role": "assistant", "content": f"[h{i}] y"})
            compacted = module.reactive_compact(list(messages))
            self.assertEqual(compacted[1:], messages[-5:])       # tail preserved verbatim
            self.assertEqual(captured["messages"], messages[:-5])  # summary only old history

    def test_make_item_deterministic_and_anomaly_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_item_under_test", S21_PATH, Path(tmp))
            self.assertEqual(module.make_item(3, seed=42), module.make_item(3, seed=42))
            for idx in range(72):
                item = module.make_item(idx, seed=42)
                self.assertNotIn("anomal", item["config"].lower())  # secret not leaked to prompt
                if item["cpu"] > 85:
                    self.assertIn("cpu", item["anomalies"])
                else:
                    self.assertNotIn("cpu", item["anomalies"])
                if item["mem"] > 90:
                    self.assertIn("mem", item["anomalies"])
                else:
                    self.assertNotIn("mem", item["anomalies"])

    def test_work_log_append_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module("s21_worklog_under_test", S21_PATH, Path(tmp))
            path = Path(tmp) / "work_log.jsonl"
            item_cpu = {"day": 1, "hour": 6, "node": "node-6", "anomalies": ["cpu"]}
            rec = module.work_log_append(path, 5, item_cpu, "anomaly: cpu 92%")
            self.assertTrue(rec["reported_anomaly"])
            self.assertTrue(rec["caught_anomaly"])
            loaded = module.read_work_log(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["index"], 5)
            # healthy node that reports anomaly -> false alarm, not caught
            ok = {"day": 1, "hour": 7, "node": "node-7", "anomalies": []}
            rec2 = module.work_log_append(path, 6, ok, "anomaly: cpu 99%")
            self.assertTrue(rec2["reported_anomaly"])
            self.assertFalse(rec2["caught_anomaly"])
            self.assertEqual(len(module.read_work_log(path)), 2)


if __name__ == "__main__":
    unittest.main()
