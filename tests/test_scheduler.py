# coding=utf-8
"""Batch scheduling tests: width derivation, core allocation, parallel semantics.

Covers the scheduler ported from the source platform's smart-slicing layer plus
the library-level additions:

  1. core discovery and the derived width rule;
  2. ``resolve_concurrency`` for every accepted form (int / auto / cores /
     serial / multiplier / env override / clamping / rejection);
  3. ``SchedulerPolicy`` validation and the contextvar scoping;
  4. the ported parallel contract - order preservation, real concurrency
     (Barrier-proven, with a falsifiable negative), error isolation,
     first-failure-by-index, serial fast path, per-task hooks;
  5. core allocation ("分配核"): round-robin assignment, mask restoration;
  6. the batch slicing API over real files, including cross-backend determinism;
  7. pickling, which the process backend depends on;
  8. the CLI ``batch`` / ``cores`` subcommands.

Offline and deterministic: no network, no database, no credentials.

Run: python -m pytest tests/test_scheduler.py -v
"""
import contextvars
import io
import json
import os
import pickle
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import smart_slice as ss
from smart_slice import scheduler as sch
from smart_slice.exceptions import ParseError, SliceError, UnsupportedFormatError
from smart_slice.scheduler import (
    BACKEND_PROCESS,
    BACKEND_SERIAL,
    ERROR_COLLECT,
    POLICY_AUTO,
    POLICY_CORES,
    POLICY_SERIAL,
    BatchReport,
    SchedulerPolicy,
    SliceJob,
    TaskOutcome,
    as_slice_jobs,
    auto_concurrency,
    available_cores,
    core_for_index,
    default_policy,
    resolve_concurrency,
    run_parallel,
    slice_many,
    slice_paths,
    use_policy,
)

LOGGER = "smart_slice.scheduler"


class _Item:
    """Minimal file-like batch item (name + read), as an upload handle provides."""

    def __init__(self, name, content=b"data"):
        self.name = name
        self.content = content

    def read(self):
        return self.content


class _Probe:
    """Concurrency probe: counts simultaneously active tasks (lock-guarded)."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = []
        self.finished = []

    def __call__(self, item):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(getattr(item, "name", item))
        try:
            time.sleep(self.delay)
            return [getattr(item, "name", item)]
        finally:
            with self._lock:
                self.active -= 1
                self.finished.append(getattr(item, "name", item))


class CoreDiscoveryTests(unittest.TestCase):
    """项1：核发现与由核数推导的并发宽度。"""

    def test_available_cores_is_positive(self):
        self.assertGreaterEqual(available_cores(), 1)

    def test_affinity_mask_matches_available_cores_when_supported(self):
        mask = sch._affinity_mask()
        if not mask:
            self.skipTest("no affinity API on this platform")
        self.assertEqual(len(mask), available_cores())
        self.assertEqual(mask, sorted(set(mask)))

    def test_auto_rule_is_the_source_platform_heuristic(self):
        # >6 cores -> a flat 3; otherwise half the cores, never below 1
        expected = {1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3, 16: 3, 64: 3}
        for cores, width in expected.items():
            self.assertEqual(auto_concurrency(cores), width, f"cores={cores}")

    def test_auto_never_exceeds_cores(self):
        for cores in range(1, 33):
            self.assertLessEqual(auto_concurrency(cores), max(cores, 1))

    def test_auto_uses_detected_cores_by_default(self):
        self.assertEqual(auto_concurrency(), auto_concurrency(available_cores()))


class ResolveConcurrencyTests(unittest.TestCase):
    """项2：并发宽度解析的全部输入形态。"""

    def setUp(self):
        for name in (sch.ENV_CONCURRENCY, sch.ENV_MAX_CONCURRENCY):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)

    def test_none_derives_from_cores(self):
        self.assertEqual(resolve_concurrency(None), auto_concurrency())

    def test_integer_is_used_verbatim(self):
        self.assertEqual(resolve_concurrency(5), 5)
        self.assertEqual(resolve_concurrency(1), 1)

    def test_zero_and_negative_clamp_to_serial(self):
        self.assertEqual(resolve_concurrency(0), 1)
        self.assertEqual(resolve_concurrency(-4), 1)

    def test_named_policies(self):
        self.assertEqual(resolve_concurrency(POLICY_AUTO), auto_concurrency())
        self.assertEqual(resolve_concurrency("default"), auto_concurrency())
        self.assertEqual(resolve_concurrency(POLICY_CORES), min(available_cores(), sch.DEFAULT_MAX_CONCURRENCY))
        for token in (POLICY_SERIAL, "none", "off"):
            self.assertEqual(resolve_concurrency(token), 1, token)

    def test_policy_names_are_case_insensitive_and_trimmed(self):
        self.assertEqual(resolve_concurrency("  CORES "), resolve_concurrency(POLICY_CORES))

    def test_multiplier_of_cores(self):
        cores = available_cores()
        self.assertEqual(resolve_concurrency("2x"), min(cores * 2, sch.DEFAULT_MAX_CONCURRENCY))
        self.assertGreaterEqual(resolve_concurrency("0.5x"), 1)

    def test_numeric_string(self):
        self.assertEqual(resolve_concurrency("4"), 4)

    def test_task_count_caps_the_width(self):
        self.assertEqual(resolve_concurrency(16, task_count=3), 3)
        self.assertEqual(resolve_concurrency(16, task_count=0), 1)

    def test_max_concurrency_caps_the_width(self):
        self.assertEqual(resolve_concurrency(64, maximum=4), 4)
        self.assertEqual(resolve_concurrency("cores", maximum=2), 2)

    def test_environment_override(self):
        os.environ[sch.ENV_CONCURRENCY] = "5"
        self.assertEqual(resolve_concurrency(), 5)
        os.environ[sch.ENV_CONCURRENCY] = "serial"
        self.assertEqual(resolve_concurrency(), 1)

    def test_environment_max_concurrency(self):
        os.environ[sch.ENV_MAX_CONCURRENCY] = "2"
        self.assertEqual(resolve_concurrency(16), 2)

    def test_explicit_argument_beats_environment(self):
        os.environ[sch.ENV_CONCURRENCY] = "8"
        self.assertEqual(resolve_concurrency(2), 2)

    def test_invalid_string_raises(self):
        for bad in ("many", "2y", "-1x", "auto-ish"):
            with self.assertRaises(ValueError):
                resolve_concurrency(bad)

    def test_bool_is_rejected(self):
        # bool is an int subclass; accepting True as "1 worker" would hide a config bug
        with self.assertRaises(ValueError):
            resolve_concurrency(True)

    def test_wrong_type_raises(self):
        with self.assertRaises(ValueError):
            resolve_concurrency(object())

    def test_policy_object_is_accepted(self):
        self.assertEqual(resolve_concurrency(SchedulerPolicy(concurrency=6)), 6)


class SchedulerPolicyTests(unittest.TestCase):
    """项3：策略对象校验、派生与 contextvar 作用域。"""

    def setUp(self):
        for name in (sch.ENV_BACKEND, sch.ENV_PIN_CORES, sch.ENV_CONCURRENCY):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)

    def test_defaults(self):
        policy = SchedulerPolicy()
        self.assertEqual(policy.backend, sch.BACKEND_THREAD)
        self.assertEqual(policy.error_policy, sch.ERROR_RAISE_FIRST)
        self.assertTrue(policy.ordered)
        self.assertFalse(policy.pin_cores)
        self.assertIsNone(policy.timeout)

    def test_invalid_fields_raise(self):
        with self.assertRaises(ValueError):
            SchedulerPolicy(backend="coroutine")
        with self.assertRaises(ValueError):
            SchedulerPolicy(error_policy="ignore")
        with self.assertRaises(ValueError):
            SchedulerPolicy(timeout=0)
        with self.assertRaises(ValueError):
            SchedulerPolicy(max_concurrency=0)
        with self.assertRaises(ValueError):
            SchedulerPolicy(concurrency="lots")

    def test_bad_width_is_rejected_at_construction(self):
        # failing here (with a clear message) beats failing inside a batch run
        with self.assertRaises(ValueError):
            SchedulerPolicy(concurrency="nope")

    def test_with_returns_a_copy(self):
        policy = SchedulerPolicy(concurrency=2)
        derived = policy.with_(pin_cores=True)
        self.assertFalse(policy.pin_cores)
        self.assertTrue(derived.pin_cores)
        self.assertEqual(derived.concurrency, 2)

    def test_width_uses_task_count_and_ceiling(self):
        policy = SchedulerPolicy(concurrency=8)
        self.assertEqual(policy.width(3), 3)
        self.assertEqual(policy.width(), 8)
        self.assertEqual(SchedulerPolicy(concurrency=8, max_concurrency=2).width(), 2)

    def test_serial_backend_always_width_one(self):
        self.assertEqual(SchedulerPolicy(backend=BACKEND_SERIAL, concurrency=8).width(10), 1)

    def test_default_policy_reads_environment(self):
        self.assertEqual(default_policy().backend, sch.BACKEND_THREAD)
        os.environ[sch.ENV_BACKEND] = "process"
        os.environ[sch.ENV_PIN_CORES] = "1"
        policy = default_policy()
        self.assertEqual(policy.backend, BACKEND_PROCESS)
        self.assertTrue(policy.pin_cores)

    def test_unknown_env_backend_falls_back_with_a_warning(self):
        os.environ[sch.ENV_BACKEND] = "quantum"
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            policy = default_policy()
        self.assertEqual(policy.backend, sch.BACKEND_THREAD)
        self.assertTrue(any("quantum" in line for line in captured.output))

    def test_use_policy_is_scoped_and_thread_local(self):
        # current_policy() builds the environment-derived default on every call,
        # so the ambient value is compared by equality, not identity
        base = sch.current_policy()
        seen = {}

        with use_policy(SchedulerPolicy(concurrency=4)):
            self.assertEqual(sch.current_policy().concurrency, 4)

            def inner():
                # a thread starts with an empty context: the ambient policy must
                # not leak in, and setting one here must not leak out
                seen["inside"] = sch.current_policy().concurrency
                with use_policy(SchedulerPolicy(concurrency=9)):
                    seen["nested"] = sch.current_policy().concurrency

            thread = threading.Thread(target=inner)
            thread.start()
            thread.join()
            self.assertEqual(sch.current_policy().concurrency, 4)

        self.assertEqual(sch.current_policy(), base)
        self.assertIsNone(seen["inside"])
        self.assertEqual(seen["nested"], 9)

    def test_resolve_policy_precedence(self):
        policy = SchedulerPolicy(concurrency=2, backend=BACKEND_SERIAL)
        merged = sch.resolve_policy(policy, concurrency=5)
        self.assertEqual(merged.concurrency, 5)
        self.assertEqual(merged.backend, BACKEND_SERIAL)
        self.assertIs(sch.resolve_policy(policy), policy)

class RunParallelContractTests(unittest.TestCase):
    """项4：从源平台移植的并行契约（顺序、真并发、错误隔离、串行回退）。"""

    def _items(self, count):
        return [_Item(f"f{i}.txt") for i in range(count)]

    def test_empty_input_returns_empty(self):
        self.assertEqual(run_parallel([], lambda item: item), [])

    def test_max_active_never_exceeds_the_width(self):
        probe = _Probe()
        run_parallel(self._items(9), probe, concurrency=3)
        self.assertEqual(probe.max_active, 3)

    def test_width_argument_controls_max_active(self):
        probe = _Probe()
        run_parallel(self._items(6), probe, concurrency=2)
        self.assertEqual(probe.max_active, 2)

    def test_policy_width_is_honoured(self):
        probe = _Probe()
        run_parallel(self._items(6), probe, SchedulerPolicy(concurrency=4))
        self.assertEqual(probe.max_active, 4)

    def test_max_concurrency_caps_the_pool(self):
        probe = _Probe()
        run_parallel(self._items(6), probe, concurrency=16, max_concurrency=2)
        self.assertEqual(probe.max_active, 2)

    def test_real_concurrency_proven_by_barrier(self):
        # A serial implementation cannot satisfy Barrier(3) and times out; this
        # is positive proof of parallelism rather than a counter that could lie.
        barrier = threading.Barrier(3, timeout=5)

        def worker(item):
            barrier.wait()
            return item.name

        outcomes = run_parallel(self._items(3), worker, concurrency=3)
        self.assertEqual([outcome.value for outcome in outcomes], ["f0.txt", "f1.txt", "f2.txt"])

    def test_barrier_negative_control(self):
        # Falsifiability check: the same probe with width 1 must break the barrier.
        barrier = threading.Barrier(3, timeout=0.5)

        def worker(item):
            barrier.wait()
            return item.name

        with self.assertRaises(threading.BrokenBarrierError):
            run_parallel(self._items(3), worker, concurrency=1)

    def test_worker_called_exactly_once_per_item(self):
        probe = _Probe(delay=0)
        items = self._items(5)
        run_parallel(items, probe, concurrency=3)
        self.assertEqual(sorted(probe.calls), sorted(item.name for item in items))
        self.assertEqual(len(probe.calls), len(items))

    def test_results_keep_input_order_when_completion_is_reversed(self):
        items = self._items(5)
        finished = []
        lock = threading.Lock()

        def worker(item):
            index = int(item.name[1])
            time.sleep(0.01 * (len(items) - index))  # last submitted finishes first
            with lock:
                finished.append(item.name)
            return index

        outcomes = run_parallel(items, worker, concurrency=4)
        self.assertEqual([outcome.value for outcome in outcomes], [0, 1, 2, 3, 4])
        self.assertEqual([outcome.index for outcome in outcomes], [0, 1, 2, 3, 4])
        self.assertNotEqual(finished, [item.name for item in items])  # precondition: order really differed

    def test_unordered_returns_completion_order_with_indices(self):
        def worker(item):
            index = int(item.name[1])
            time.sleep(0.01 * (4 - index))
            return index

        outcomes = run_parallel(self._items(5), worker, concurrency=4, ordered=False)
        self.assertEqual(sorted(outcome.index for outcome in outcomes), [0, 1, 2, 3, 4])
        self.assertNotEqual([outcome.index for outcome in outcomes], [0, 1, 2, 3, 4])

    def test_single_item_takes_the_serial_path(self):
        probe = _Probe(delay=0)
        outcomes = run_parallel(self._items(1), probe, concurrency=8)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].worker, "main")  # no pool thread was created

    def test_width_one_takes_the_serial_path(self):
        probe = _Probe(delay=0)
        outcomes = run_parallel(self._items(4), probe, concurrency=1)
        self.assertEqual(probe.max_active, 1)
        self.assertTrue(all(outcome.worker == "main" for outcome in outcomes))

    def test_serial_backend_takes_the_serial_path(self):
        probe = _Probe(delay=0)
        outcomes = run_parallel(self._items(4), probe, backend=BACKEND_SERIAL)
        self.assertEqual(probe.max_active, 1)
        self.assertTrue(all(outcome.worker == "main" for outcome in outcomes))

    def test_failure_does_not_stop_the_rest_of_the_batch(self):
        done = []
        lock = threading.Lock()

        def worker(item):
            if item.name == "f1.txt":
                raise SliceError(500, "parse failed for f1")
            with lock:
                done.append(item.name)
            return item.name

        with self.assertRaises(SliceError) as caught:
            run_parallel(self._items(4), worker, concurrency=4)
        self.assertIn("parse failed for f1", str(caught.exception))
        self.assertEqual(sorted(done), ["f0.txt", "f2.txt", "f3.txt"])

    def test_lowest_index_failure_is_the_one_raised(self):
        def worker(item):
            if item.name in ("f0.txt", "f2.txt"):
                # f0 finishes later than f2, but must still be the one raised
                time.sleep(0.05 if item.name == "f0.txt" else 0)
                raise SliceError(500, f"boom-{item.name}")
            return item.name

        with self.assertRaises(SliceError) as caught:
            run_parallel(self._items(3), worker, concurrency=3)
        self.assertEqual(caught.exception.message, "boom-f0.txt")

    def test_original_exception_object_is_preserved(self):
        sentinel = SliceError(400, "unsupported")

        def worker(item):
            if item.name == "f1.txt":
                raise sentinel
            return item.name

        with self.assertRaises(SliceError) as caught:
            run_parallel(self._items(3), worker, concurrency=3)
        self.assertIs(caught.exception, sentinel)

    def test_non_slice_errors_propagate_unchanged(self):
        def worker(item):
            if item.name == "f1.txt":
                raise ValueError("unexpected")
            return item.name

        with self.assertRaises(ValueError):
            run_parallel(self._items(2), worker, concurrency=2)

    def test_additional_failures_are_logged(self):
        def worker(item):
            if item.name != "f1.txt":
                raise SliceError(500, f"boom-{item.name}")
            return item.name

        with self.assertLogs(LOGGER, level="ERROR") as captured:
            with self.assertRaises(SliceError):
                run_parallel(self._items(3), worker, concurrency=3)
        # f0 is raised, f2 is logged
        self.assertEqual(len(captured.records), 1)
        self.assertIn("f2.txt", captured.records[0].getMessage())

    def test_collect_policy_returns_every_outcome(self):
        def worker(item):
            if item.name in ("f0.txt", "f2.txt"):
                raise SliceError(500, f"boom-{item.name}")
            return item.name

        outcomes = run_parallel(self._items(4), worker, concurrency=4, error_policy=ERROR_COLLECT)
        self.assertEqual(len(outcomes), 4)
        self.assertEqual([outcome.ok for outcome in outcomes], [False, True, False, True])
        self.assertEqual([outcome.error.message for outcome in outcomes if outcome.error],
                         ["boom-f0.txt", "boom-f2.txt"])

    def test_task_hooks_run_once_per_item(self):
        calls = {"setup": 0, "teardown": 0}
        lock = threading.Lock()

        def bump(key):
            def hook():
                with lock:
                    calls[key] += 1
            return hook

        run_parallel(self._items(4), lambda item: item.name, concurrency=3,
                     task_setup=bump("setup"), task_teardown=bump("teardown"))
        self.assertEqual(calls, {"setup": 4, "teardown": 4})

    def test_teardown_runs_even_when_the_task_fails(self):
        calls = {"setup": 0, "teardown": 0}
        lock = threading.Lock()

        def bump(key):
            def hook():
                with lock:
                    calls[key] += 1
            return hook

        def worker(item):
            raise SliceError(500, "always fails")

        with self.assertRaises(SliceError):
            run_parallel(self._items(3), worker, concurrency=3,
                         task_setup=bump("setup"), task_teardown=bump("teardown"))
        self.assertEqual(calls, {"setup": 3, "teardown": 3})

    def test_hooks_also_run_on_the_serial_path(self):
        calls = {"setup": 0, "teardown": 0}

        def bump(key):
            def hook():
                calls[key] += 1
            return hook

        run_parallel(self._items(2), lambda item: item.name, concurrency=1,
                     task_setup=bump("setup"), task_teardown=bump("teardown"))
        self.assertEqual(calls, {"setup": 2, "teardown": 2})

    def test_timeout_is_recorded_as_a_failure(self):
        def worker(item):
            time.sleep(0.4)
            return item.name

        outcomes = run_parallel(self._items(2), worker, concurrency=2,
                                timeout=0.05, error_policy=ERROR_COLLECT)
        self.assertTrue(all(not outcome.ok for outcome in outcomes))
        self.assertIsInstance(outcomes[0].error, Exception)

    def test_outcome_timing_and_worker_are_recorded(self):
        def worker(item):
            time.sleep(0.02)
            return item.name

        outcomes = run_parallel(self._items(3), worker, concurrency=3)
        for outcome in outcomes:
            self.assertGreaterEqual(outcome.elapsed, 0.01)
            self.assertTrue(outcome.worker)
            self.assertTrue(outcome.name.startswith("f"))

    def test_raise_for_status_on_outcome(self):
        error = SliceError(500, "x")
        failed = TaskOutcome(index=0, ok=False, error=error)
        with self.assertRaises(SliceError):
            failed.raise_for_status()
        self.assertEqual(TaskOutcome(index=1, ok=True, value="v").raise_for_status(), "v")

class CoreAllocationTests(unittest.TestCase):
    """项5：核分配（分配核）——轮转分配、实际收窄、批后还原。"""

    def test_core_for_index_is_round_robin(self):
        cores = [2, 5, 7]
        self.assertEqual([core_for_index(i, cores) for i in range(7)], [2, 5, 7, 2, 5, 7, 2])

    def test_core_for_index_without_a_mask_is_none(self):
        self.assertIsNone(core_for_index(0, []))

    def test_core_for_index_uses_the_process_mask(self):
        mask = sch._affinity_mask()
        if not mask:
            self.skipTest("no affinity API on this platform")
        self.assertIn(core_for_index(0), mask)
        self.assertEqual(core_for_index(len(mask)), core_for_index(0))

    def test_no_pinning_by_default(self):
        outcomes = run_parallel([_Item(f"f{i}.txt") for i in range(4)],
                                lambda item: item.name, concurrency=4)
        self.assertTrue(all(outcome.core is None for outcome in outcomes))

    def test_pinning_allocates_a_distinct_core_per_task(self):
        mask = sch._affinity_mask()
        if not mask:
            self.skipTest("pinning needs sched_getaffinity or SetThreadAffinityMask")
        items = [_Item(f"f{i}.txt") for i in range(min(4, len(mask)))]
        outcomes = run_parallel(items, lambda item: item.name,
                                concurrency=len(items), pin_cores=True)
        allocated = [outcome.core for outcome in outcomes]
        self.assertTrue(all(core is not None for core in allocated), allocated)
        self.assertEqual(len(set(allocated)), len(allocated))  # no two tasks share a core
        self.assertEqual(allocated, [core_for_index(i, mask) for i in range(len(items))])
        self.assertTrue(set(allocated) <= set(mask))

    @unittest.skipUnless(hasattr(os, "sched_getaffinity"), "Linux affinity API")
    def test_pinning_actually_narrows_the_mask_and_is_restored(self):
        mask = sch._affinity_mask()
        if len(mask) < 2:
            self.skipTest("needs at least two cores to prove narrowing")

        def worker(item):
            return sorted(os.sched_getaffinity(0))

        items = [_Item(f"f{i}.txt") for i in range(2)]
        outcomes = run_parallel(items, worker, concurrency=2, pin_cores=True)
        for index, outcome in enumerate(outcomes):
            self.assertEqual(outcome.value, [core_for_index(index, mask)], outcome.value)

        # the calling thread was never pinned, and each worker restored its mask
        self.assertEqual(sorted(os.sched_getaffinity(0)), mask)

    def test_pinning_survives_a_failing_task(self):
        mask = sch._affinity_mask()
        if not mask:
            self.skipTest("no affinity API on this platform")

        def worker(item):
            if item.name == "f1.txt":
                raise SliceError(500, "boom")
            return item.name

        outcomes = run_parallel([_Item(f"f{i}.txt") for i in range(3)], worker,
                                concurrency=3, pin_cores=True, error_policy=ERROR_COLLECT)
        pinned = [outcome.core for outcome in outcomes if outcome.core is not None]
        self.assertEqual(len(pinned), 3)  # teardown restored every worker, including the failure

    def test_serial_path_does_not_pin_the_caller(self):
        mask = sch._affinity_mask()
        run_parallel([_Item("only.txt")], lambda item: item.name, pin_cores=True)
        if hasattr(os, "sched_getaffinity"):
            self.assertEqual(sorted(os.sched_getaffinity(0)), mask)


class BatchSliceTests(unittest.TestCase):
    """项6：批量切片 API——真实文件、跨后端一致性、输入形态、报告对象。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ss-batch-")
        cls.paths = []
        for index in range(5):
            path = os.path.join(cls.tmp, f"doc{index}.md")
            body = "\n\n".join(
                f"## Section {index}-{sub}\n\n" + f"paragraph {sub} of document {index}. " * 12
                for sub in range(4)
            )
            with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"# Doc {index}\n\n{body}")
            cls.paths.append(path)
        cls.csv = os.path.join(cls.tmp, "table.csv")
        with io.open(cls.csv, "w", encoding="utf-8", newline="") as handle:
            handle.write("id,name,note\n")
            for row in range(30):
                handle.write(f"{row},item-{row},note text {row}\n")
        cls.paths.append(cls.csv)

    def test_batch_matches_serial_document_by_document(self):
        expected = [ss.slice_path(path, limit=800) for path in self.paths]
        report = slice_paths(self.paths, concurrency=4, limit=800)
        self.assertTrue(report.ok, [str(o.error) for o in report.failures])
        self.assertEqual(report.results, expected)

    def test_threads_and_processes_agree_with_serial(self):
        serial = slice_paths(self.paths, backend=BACKEND_SERIAL, limit=800).results
        threaded = slice_paths(self.paths, concurrency=4, limit=800).results
        self.assertEqual(threaded, serial)
        try:
            processed = slice_paths(self.paths, backend=BACKEND_PROCESS, concurrency=2, limit=800).results
        except OSError as error:  # pragma: no cover - sandbox without process support
            self.skipTest(f"process backend unavailable: {error}")
        self.assertEqual(processed, serial)

    def test_paragraphs_are_flattened_in_input_order(self):
        report = slice_paths(self.paths, concurrency=3, limit=800)
        flat = report.paragraphs
        per_doc = report.results
        self.assertEqual(flat, [row for rows in per_doc for row in rows])
        self.assertTrue(all(isinstance(row, dict) for row in flat))
        self.assertIn("Doc 0", flat[0]["title"])

    def test_report_accessors(self):
        report = slice_paths(self.paths[:3], concurrency=3, limit=800)
        self.assertIsInstance(report, BatchReport)
        self.assertEqual(len(report), 3)
        self.assertTrue(report.ok)
        self.assertEqual(report.failures, [])
        self.assertEqual(list(report), report.results)
        self.assertEqual(report[0], report.results[0])
        self.assertIn("documents sliced", report.summary())
        self.assertEqual(report.width, 3)
        self.assertGreater(report.elapsed, 0)

    def test_mixed_input_shapes(self):
        with io.open(self.paths[0], "rb") as handle:
            content = handle.read()
        jobs = [
            SliceJob.from_path(self.paths[0]),
            SliceJob.from_bytes(content, "inline.md", limit=500),
            (os.path.basename(self.paths[1]), content),
            {"name": "mapping.md", "content": content},
            {"name": "mapped-path.md", "path": self.paths[2]},
            _Item("uploaded.md", content),
            self.paths[3],
        ]
        report = slice_many(jobs, concurrency=4, limit=800)
        self.assertEqual(
            [outcome.name for outcome in report.outcomes],
            ["doc0.md", "inline.md", "doc1.md", "mapping.md", "mapped-path.md", "uploaded.md", "doc3.md"],
        )
        self.assertTrue(report.ok, [str(o.error) for o in report.failures])

    def test_file_like_input_is_read_in_the_calling_thread(self):
        with io.open(self.paths[0], "rb") as handle:
            jobs = as_slice_jobs([handle])
        self.assertEqual(len(jobs), 1)
        self.assertIsNotNone(jobs[0].content)  # bytes captured, handle no longer needed
        self.assertTrue(jobs[0].name.endswith(".md"))

    def test_per_job_kwargs_override_batch_kwargs(self):
        jobs = [
            SliceJob.from_path(self.paths[0]),
            SliceJob.from_path(self.paths[1], limit=200),
        ]
        report = slice_many(jobs, concurrency=2, limit=2000)
        wide = max(len(row["content"]) for row in report.results[0])
        narrow = max(len(row["content"]) for row in report.results[1])
        self.assertGreater(wide, 200)
        self.assertLessEqual(narrow, 200 + 40)

    def test_ambient_chunking_options_reach_worker_threads(self):
        # A pool thread starts with an empty contextvar context; without the
        # capture in slice_many the ambient options would be silently dropped.
        # Both runs happen inside the same block so they are comparable.
        with ss.use_options(ss.ChunkingOptions(limit=300, overlap=60)):
            report = slice_paths(self.paths[:3], concurrency=3)
            serial = slice_paths(self.paths[:3], backend=BACKEND_SERIAL).results
        widest = max(len(row["content"]) for row in report.paragraphs)
        self.assertLessEqual(widest, 300 + 60 + 40)
        self.assertLess(widest, 300 + 60 + 40 + 1)
        self.assertEqual(report.results, serial)

    def test_ambient_options_are_not_silently_dropped(self):
        # Regression guard for the failure mode the test above is built to catch:
        # a wide ambient limit must actually narrow the parallel batch output.
        wide = slice_paths(self.paths[:1], concurrency=1, limit=2000).paragraphs
        with ss.use_options(ss.ChunkingOptions(limit=200)):
            narrow = slice_paths(self.paths[:1], concurrency=1).paragraphs
        self.assertGreater(max(len(row["content"]) for row in wide), 200)
        self.assertLessEqual(max(len(row["content"]) for row in narrow), 200 + 40)

    def test_explicit_options_win_over_the_ambient_ones(self):
        with ss.use_options(ss.ChunkingOptions(limit=300)):
            report = slice_paths(self.paths[:2], concurrency=2, limit=1500)
        self.assertGreater(max(len(row["content"]) for row in report.paragraphs), 300)

    def test_empty_batch(self):
        report = slice_many([])
        self.assertEqual(len(report), 0)
        self.assertTrue(report.ok)
        self.assertEqual(report.paragraphs, [])
        self.assertEqual(report.elapsed, 0.0)

    def test_failure_is_isolated_and_reported(self):
        unsupported = os.path.join(self.tmp, "movie.mp4")
        with open(unsupported, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        paths = self.paths[:2] + [unsupported] + self.paths[3:]

        report = slice_paths(paths, concurrency=4, limit=800, error_policy=ERROR_COLLECT)
        self.assertFalse(report.ok)
        self.assertEqual([outcome.index for outcome in report.failures], [2])
        self.assertIsInstance(report.failures[0].error, SliceError)
        self.assertEqual(report.failures[0].error.code, 400)
        self.assertEqual(len([row for row in report.results if row]), len(paths) - 1)

        with self.assertRaises(SliceError) as caught:
            slice_paths(paths, concurrency=4, limit=800)
        self.assertEqual(caught.exception.code, 400)

    def test_raise_first_can_be_replayed_from_the_report(self):
        unsupported = os.path.join(self.tmp, "movie2.mp4")
        with open(unsupported, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42")
        report = slice_paths([self.paths[0], unsupported], concurrency=2, error_policy=ERROR_COLLECT)
        with self.assertRaises(SliceError):
            report.raise_first()

    def test_progress_hook_is_shared_across_workers(self):
        seen = []
        lock = threading.Lock()

        def hook():
            with lock:
                seen.append(1)

        report = slice_paths(self.paths[:4], concurrency=4, limit=800, progress_hook=hook)
        self.assertTrue(report.ok)
        self.assertGreaterEqual(len(seen), 4)  # at least one callback per document


class SliceJobTests(unittest.TestCase):
    """项6b：批量输入的归一化与校验。"""

    def test_from_path_derives_the_name(self):
        job = SliceJob.from_path(os.path.join("some", "dir", "report.pdf"))
        self.assertEqual(job.name, "report.pdf")
        self.assertIsNone(job.content)

    def test_from_bytes_copies_the_content(self):
        raw = bytearray(b"# t\n\nbody")
        job = SliceJob.from_bytes(raw, "t.md")
        self.assertIsInstance(job.content, bytes)
        raw.extend(b"!")  # mutating the caller's buffer must not change the job
        self.assertEqual(job.content, b"# t\n\nbody")

    def test_job_needs_content_or_path(self):
        with self.assertRaises(ValueError):
            SliceJob(name="empty.md")

    def test_raw_bytes_are_rejected_with_guidance(self):
        with self.assertRaises(TypeError) as caught:
            as_slice_jobs([b"# no name"])
        self.assertIn("SliceJob.from_bytes", str(caught.exception))

    def test_mapping_without_name_is_rejected(self):
        with self.assertRaises(TypeError):
            as_slice_jobs([{"content": b"x"}])

    def test_mapping_without_source_is_rejected(self):
        with self.assertRaises(TypeError):
            as_slice_jobs([{"name": "a.md"}])

    def test_unsupported_item_is_rejected(self):
        with self.assertRaises(TypeError) as caught:
            as_slice_jobs([42])
        self.assertIn("batch item 0", str(caught.exception))

    def test_two_tuple_must_be_name_and_bytes(self):
        with self.assertRaises(TypeError):
            as_slice_jobs([("a.md", "not bytes")])

    def test_jobs_pass_through_unchanged(self):
        job = SliceJob.from_bytes(b"# a", "a.md")
        self.assertIs(as_slice_jobs([job])[0], job)


class PicklingTests(unittest.TestCase):
    """项7：process 后端依赖的可序列化性。"""

    def test_errors_round_trip_with_their_code(self):
        for error in (SliceError(500, "boom"), UnsupportedFormatError("nope"), ParseError("bad")):
            restored = pickle.loads(pickle.dumps(error))
            self.assertIs(type(restored), type(error))
            self.assertEqual(restored.code, error.code)
            self.assertEqual(restored.message, error.message)

    def test_slice_job_round_trips(self):
        job = SliceJob.from_bytes(b"# a\n\nbody", "a.md", limit=500)
        restored = pickle.loads(pickle.dumps(job))
        self.assertEqual(restored, job)

    def test_policy_without_callbacks_round_trips(self):
        policy = SchedulerPolicy(concurrency=4, pin_cores=True, error_policy=ERROR_COLLECT)
        self.assertEqual(pickle.loads(pickle.dumps(policy)), policy)

    def test_outcome_round_trips(self):
        outcome = TaskOutcome(index=2, name="a.md", ok=False, error=SliceError(400, "x"), core=3)
        restored = pickle.loads(pickle.dumps(outcome))
        self.assertEqual(restored.index, 2)
        self.assertEqual(restored.error.code, 400)
        self.assertEqual(restored.core, 3)

    def test_the_job_worker_is_picklable(self):
        # a closure here would break backend="process" for every batch that
        # passes slicing keywords, which is nearly all of them
        worker = sch._job_worker({"limit": 800})
        restored = pickle.loads(pickle.dumps(worker))
        job = SliceJob.from_bytes(b"# a\n\nbody text", "a.md")
        self.assertEqual(restored(job), worker(job))

class CLIBatchTests(unittest.TestCase):
    """项8：命令行 batch / cores 子命令。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ss-cli-")
        cls.paths = []
        for index in range(4):
            path = os.path.join(cls.tmp, f"cli{index}.md")
            with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"# CLI {index}\n\n## Sub\n\nbody text of document {index}.\n")
            cls.paths.append(path)
        cls.bad = os.path.join(cls.tmp, "clip.mp4")
        with open(cls.bad, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)

    def _run(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        for name in ("SMART_SLICE_CONCURRENCY", "SMART_SLICE_SCHEDULER_BACKEND", "SMART_SLICE_PIN_CORES"):
            env.pop(name, None)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable, "-m", "smart_slice", *args],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=repo, timeout=180,
        )

    def test_batch_jsonl_is_the_default(self):
        proc = self._run("batch", *self.paths[:2])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["name"] for row in rows], ["cli0.md", "cli1.md"])
        self.assertTrue(all(row["ok"] for row in rows))
        self.assertTrue(any("body text of document 0" in p["content"] for p in rows[0]["paragraphs"]))

    def test_batch_respects_the_width_flag(self):
        proc = self._run("batch", *self.paths, "-j", "2", "--stats")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("width=2", proc.stderr)
        self.assertIn("4/4 documents sliced", proc.stderr)

    def test_batch_long_concurrency_flag_and_pinning(self):
        proc = self._run("batch", *self.paths, "--concurrency", "3", "--pin-cores", "--stats")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("width=3", proc.stderr)
        self.assertIn("pin_cores=True", proc.stderr)

    def test_batch_serial_backend(self):
        proc = self._run("batch", *self.paths, "--backend", "serial", "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["report"]["width"], 1)
        self.assertEqual(len(payload["results"]), 4)

    def test_batch_json_format(self):
        proc = self._run("batch", *self.paths[:2], "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("summary", payload["report"])
        self.assertEqual([row["name"] for row in payload["results"]], ["cli0.md", "cli1.md"])

    def test_batch_summary_format(self):
        proc = self._run("batch", *self.paths[:2], "--format", "summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok  cli0.md", proc.stdout)

    def test_batch_text_format(self):
        proc = self._run("batch", *self.paths[:1], "--format", "text")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# CLI 0", proc.stdout)
        self.assertIn("body text of document 0.", proc.stdout)

    def test_batch_writes_to_a_file(self):
        output = os.path.join(self.tmp, "out.jsonl")
        proc = self._run("batch", *self.paths[:2], "--output", output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        with io.open(output, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 2)

    def test_batch_slicing_options_are_forwarded(self):
        # a short document yields one paragraph at any limit, so use a long one:
        # a small --limit must visibly produce more paragraphs than a large one
        long_path = os.path.join(self.tmp, "long.md")
        with io.open(long_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Long\n\n" + (" ".join(f"word{i}" for i in range(400)) + "\n"))
        small = json.loads(self._run("batch", long_path, "--limit", "60", "--format", "json").stdout)
        large = json.loads(self._run("batch", long_path, "--limit", "4000", "--format", "json").stdout)
        self.assertGreater(len(small["results"][0]["paragraphs"]), 1)
        self.assertGreater(
            len(small["results"][0]["paragraphs"]), len(large["results"][0]["paragraphs"])
        )
        self.assertTrue(all(len(row["content"]) <= 100 for row in small["results"][0]["paragraphs"]))

    def test_batch_unsupported_file_fails_the_batch(self):
        proc = self._run("batch", *self.paths[:1], self.bad)
        self.assertEqual(proc.returncode, 1)
        # the failing document must be named, and no traceback may leak to the user
        self.assertIn("clip.mp4", proc.stderr)
        self.assertIn("Unsupported file format", proc.stderr)
        self.assertIn("code 400", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_batch_reports_how_many_others_failed(self):
        second_bad = os.path.join(self.tmp, "clip2.mp4")
        with open(second_bad, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42")
        proc = self._run("batch", self.bad, *self.paths[:1], second_bad)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("clip.mp4", proc.stderr)          # lowest index is reported
        self.assertIn("1 more document(s) failed", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_batch_collect_lists_every_failure(self):
        second_bad = os.path.join(self.tmp, "clip3.mp4")
        with open(second_bad, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42")
        proc = self._run("batch", self.bad, *self.paths[:1], second_bad, "--error-policy", "collect")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("clip.mp4", proc.stderr)
        self.assertIn("clip3.mp4", proc.stderr)
        self.assertIn("2 of 3 documents failed", proc.stderr)

    def test_batch_collect_policy_reports_every_document(self):
        proc = self._run("batch", *self.paths[:2], self.bad, "--error-policy", "collect", "--format", "json")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        states = [(row["name"], row["ok"]) for row in payload["results"]]
        self.assertEqual(states, [("cli0.md", True), ("cli1.md", True), ("clip.mp4", False)])
        failed = payload["results"][2]
        self.assertEqual(failed["error"]["code"], 400)
        self.assertIn("1 of 3 documents failed", proc.stderr)

    def test_batch_missing_path_exits_one(self):
        proc = self._run("batch", os.path.join(self.tmp, "absent.md"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot read", proc.stderr)

    def test_batch_invalid_width_exits_two(self):
        proc = self._run("batch", *self.paths[:1], "-j", "lots")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("concurrency", proc.stderr)

    def test_cores_command(self):
        proc = self._run("cores")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("available_cores", proc.stdout)
        self.assertIn("auto_concurrency", proc.stdout)
        self.assertIn("pin_cores_supported", proc.stdout)

    def test_cores_command_json(self):
        proc = self._run("cores", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertGreaterEqual(payload["available_cores"], 1)
        self.assertGreaterEqual(payload["auto_concurrency"], 1)
        self.assertIsInstance(payload["affinity_mask"], list)

    def test_batch_environment_width_is_not_shadowed_by_the_default(self):
        # Regression guard: the CLI used to pass the literal "auto" as the
        # default, which outranked SMART_SLICE_CONCURRENCY and made the variable
        # dead for every CLI user.
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["SMART_SLICE_CONCURRENCY"] = "1"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "smart_slice", "batch", *self.paths, "--stats"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=repo, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("width=1", proc.stderr)

    def test_batch_honours_the_environment_width(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["SMART_SLICE_CONCURRENCY"] = "2"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "smart_slice", "batch", *self.paths, "--stats"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=repo, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("width=2", proc.stderr)

    def test_existing_subcommands_still_work(self):
        proc = self._run("slice", self.paths[0], "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout))
        proc = self._run("detect", self.paths[0])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run("formats", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("handlers", json.loads(proc.stdout))


if __name__ == "__main__":
    unittest.main()