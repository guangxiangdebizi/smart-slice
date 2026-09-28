# coding=utf-8
"""Batch scheduling: how many documents to slice at once, and on which cores.

Slicing one document is a single-threaded walk over its text.  Ingesting a
corpus is a different problem: hundreds of independent files on a machine with
several cores.  This module is the scheduler for that case - the concurrency
allocation layer the source platform ran under its upload and bulk-import
paths, generalised into a dependency-free form.

Ported semantics (behaviour-compatible with the source platform's batch path)
---------------------------------------------------------------------------
* **input order is the output order** - results are back-filled by submission
  index, never appended in completion order, so a caller can zip the results
  against its input list;
* **error isolation** - one failing document does not abort the others; every
  submitted task runs to completion before failures are judged;
* **first-failure-wins** - with the default ``error_policy="raise_first"`` the
  exception raised is the one belonging to the *earliest input index* that
  failed (not whichever finished first), re-raised as the original object so
  its traceback survives the thread hop; every other failure is logged;
* **serial fast path** - a single item, or a resolved width of 1, never builds
  a pool: same call, same result, no threads;
* **per-task hooks** - the platform cleaned up its thread-local database
  connections around every task; that coupling is generalised into
  ``task_setup`` / ``task_teardown`` callbacks so a host framework can plug in
  its own cleanup.

What changed on the way here
----------------------------
* **the width is derived, not hard-coded.**  The platform pinned its batch
  width to a constant that was hand-aligned with the rest of its pipeline.  A
  library cannot know its host's pipeline, so ``"auto"`` derives the width from
  the cores actually available to this process using the platform's own
  allocation rule (3 above six cores, otherwise half of them, never below 1).
  ``SMART_SLICE_CONCURRENCY`` and the ``concurrency=`` argument override it.
* **core pinning is opt-in** (``pin_cores=True``, Linux only): each task is
  bound to one core of the process affinity mask, round-robin, and the
  original mask is restored when the task ends.
* **a process backend** (``backend="process"``) exists for CPU-bound corpora.
  Slicing is pure Python, so threads only overlap the GIL-releasing work inside
  the C parsers; processes scale with cores.  Threads remain the default
  because they accept unpicklable arguments (callbacks, tokenizers) and carry
  no pool-startup or ``spawn`` caveats.
"""
import contextvars
import functools
import os
import threading
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ._config import _env_flag, integer_setting, setting
from ._i18n import gettext as _
from ._logging import get_logger

_log = get_logger("scheduler")

__all__ = [
    "BACKEND_PROCESS",
    "BACKEND_SERIAL",
    "BACKEND_THREAD",
    "BACKENDS",
    "DEFAULT_MAX_CONCURRENCY",
    "ENV_BACKEND",
    "ENV_CONCURRENCY",
    "ENV_MAX_CONCURRENCY",
    "ENV_PIN_CORES",
    "ERROR_COLLECT",
    "ERROR_POLICIES",
    "ERROR_RAISE_FIRST",
    "POLICY_AUTO",
    "POLICY_CORES",
    "POLICY_SERIAL",
    "BatchReport",
    "SchedulerPolicy",
    "SliceJob",
    "TaskOutcome",
    "as_slice_jobs",
    "auto_concurrency",
    "available_cores",
    "current_policy",
    "default_policy",
    "resolve_concurrency",
    "resolve_policy",
    "run_parallel",
    "slice_many",
    "slice_paths",
    "use_policy",
]

#: Environment overrides, resolved per call (never cached at import time).
ENV_CONCURRENCY = "SMART_SLICE_CONCURRENCY"
ENV_MAX_CONCURRENCY = "SMART_SLICE_MAX_CONCURRENCY"
ENV_BACKEND = "SMART_SLICE_SCHEDULER_BACKEND"
ENV_PIN_CORES = "SMART_SLICE_PIN_CORES"

BACKEND_THREAD = "thread"
BACKEND_PROCESS = "process"
BACKEND_SERIAL = "serial"
BACKENDS = (BACKEND_THREAD, BACKEND_PROCESS, BACKEND_SERIAL)

#: Named widths accepted wherever an integer is accepted.
POLICY_AUTO = "auto"
POLICY_CORES = "cores"
POLICY_SERIAL = "serial"
_WIDTH_WORDS = (POLICY_AUTO, POLICY_CORES, POLICY_SERIAL, "default", "none", "off")

ERROR_RAISE_FIRST = "raise_first"
ERROR_COLLECT = "collect"
ERROR_POLICIES = (ERROR_RAISE_FIRST, ERROR_COLLECT)

#: Defensive ceiling for a derived width.  Every concurrent task holds its own
#: document bytes plus parser buffers, so an unbounded width on a many-core host
#: trades a little latency for a lot of RSS.  ``max_concurrency`` overrides it.
DEFAULT_MAX_CONCURRENCY = 32


# --------------------------------------------------------------------------- #
# core discovery and width derivation
# --------------------------------------------------------------------------- #
def available_cores() -> int:
    """CPU cores this process may actually run on (never below 1).

    Linux reports the affinity mask, so ``taskset``/cgroup pinning is honoured
    instead of the host's core count.  Elsewhere ``os.process_cpu_count()``
    (3.13+) or ``os.cpu_count()`` is used.
    """
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is not None:
        try:
            count = len(get_affinity(0))
        except OSError:  # pragma: no cover - unusual kernel/permission state
            count = 0
        if count > 0:
            return count
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        try:
            count = process_cpu_count() or 0
        except (OSError, ValueError):  # pragma: no cover - defensive
            count = 0
        if count > 0:
            return count
    return max(1, os.cpu_count() or 1)


def auto_concurrency(cores: Optional[int] = None) -> int:
    """The derived batch width for ``cores`` available cores.

    This is the source platform's worker allocation rule, kept verbatim so the
    default behaves like the system the scheduler was extracted from: a flat 3
    on a machine with more than six cores (leaving headroom for everything else
    the host runs), half the cores below that, and never fewer than 1.
    """
    count = available_cores() if cores is None else max(1, int(cores))
    return 3 if count > 6 else max(1, count // 2)

def resolve_concurrency(
    concurrency: Union[None, int, str, "SchedulerPolicy"] = None,
    *,
    task_count: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Turn a user-supplied width into the integer actually used.

    Accepted forms, in the order they are checked:

    ``None``
        the environment (``SMART_SLICE_CONCURRENCY``), else :func:`auto_concurrency`;
    ``int``
        that width, clamped to ``>= 1``;
    ``"auto"`` / ``"default"``
        :func:`auto_concurrency`;
    ``"cores"``
        every available core;
    ``"serial"`` / ``"none"`` / ``"off"`` / ``1`` / ``0``
        1 - the serial fast path, no pool at all;
    ``"2x"`` / ``"0.5x"``
        a multiple of the available cores (at least 1).

    The result is capped by ``maximum`` (default ``SMART_SLICE_MAX_CONCURRENCY``,
    else :data:`DEFAULT_MAX_CONCURRENCY`) and, when ``task_count`` is known, by
    the number of tasks - spinning up eight workers for three files is waste.
    An unrecognised string raises :class:`ValueError` rather than silently
    falling back, so a typo in a config file is visible.
    """
    if isinstance(concurrency, SchedulerPolicy):
        concurrency = concurrency.concurrency

    if concurrency is None:
        raw: Any = setting(ENV_CONCURRENCY, None)
        concurrency = raw if raw not in (None, "") else POLICY_AUTO

    width: int
    if isinstance(concurrency, bool):  # bool is an int subclass; reject it explicitly
        raise ValueError("concurrency must be an int or a policy name, not a bool")
    if isinstance(concurrency, int):
        width = int(concurrency)
    elif isinstance(concurrency, str):
        token = concurrency.strip().lower()
        if token in ("", POLICY_AUTO, "default"):
            width = auto_concurrency()
        elif token in (POLICY_SERIAL, "none", "off"):
            width = 1
        elif token == POLICY_CORES:
            width = available_cores()
        elif token.endswith("x"):
            try:
                factor = float(token[:-1])
            except ValueError:
                raise ValueError(f"unknown concurrency policy: {concurrency!r}") from None
            if factor <= 0:
                raise ValueError(f"concurrency multiplier must be positive: {concurrency!r}")
            width = int(round(available_cores() * factor))
        else:
            try:
                width = int(token)
            except ValueError:
                raise ValueError(f"unknown concurrency policy: {concurrency!r}") from None
    else:
        raise ValueError(f"concurrency must be an int or a string, got {type(concurrency).__name__}")

    if width < 1:
        width = 1

    ceiling = maximum
    if ceiling is None:
        ceiling = integer_setting(ENV_MAX_CONCURRENCY, DEFAULT_MAX_CONCURRENCY, minimum=1)
    width = min(width, max(1, int(ceiling)))

    if task_count is not None:
        width = min(width, max(1, int(task_count)))
    return width


# --------------------------------------------------------------------------- #
# policy object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SchedulerPolicy:
    """Immutable batch-scheduling configuration.

    :param concurrency:    how wide to run - an int, or ``"auto"`` / ``"cores"``
                           / ``"serial"`` / ``"2x"`` (see :func:`resolve_concurrency`).
                           ``None`` reads ``SMART_SLICE_CONCURRENCY``, else auto.
    :param backend:        ``"thread"`` (default), ``"process"`` or ``"serial"``.
                           Threads share memory and accept callbacks; processes
                           bypass the GIL for CPU-bound corpora but need picklable
                           arguments and pay pool startup.
    :param max_concurrency: hard ceiling for a derived width.
    :param pin_cores:      bind each task to one core of the process affinity
                           mask (Linux only; a documented no-op elsewhere).
    :param error_policy:   ``"raise_first"`` (default) reproduces the platform's
                           whole-batch failure semantics; ``"collect"`` returns
                           per-item outcomes and never raises.
    :param ordered:        keep results in input order (default True).  With
                           False, results arrive in completion order and each
                           carries its input ``index``.
    :param task_setup:     zero-arg callback run inside the worker before each
                           task; ``task_teardown`` runs after it in a ``finally``.
                           This is where a host framework releases per-thread
                           resources (the source platform closed stale database
                           connections here).
    :param timeout:        per-task wall-clock seconds.  ``None`` waits forever.
                           A timeout is recorded as that task's failure; with
                           threads the worker keeps running in the background
                           (Python cannot kill a thread), with processes the
                           worker is torn down.
    :param thread_name_prefix: pool thread naming, for log/profiler readability.
    """

    concurrency: Union[None, int, str] = None
    backend: str = BACKEND_THREAD
    max_concurrency: Optional[int] = None
    pin_cores: bool = False
    error_policy: str = ERROR_RAISE_FIRST
    ordered: bool = True
    task_setup: Optional[Callable[[], None]] = None
    task_teardown: Optional[Callable[[], None]] = None
    timeout: Optional[float] = None
    thread_name_prefix: str = "smart-slice"

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}, got {self.backend!r}")
        if self.error_policy not in ERROR_POLICIES:
            raise ValueError(f"error_policy must be one of {ERROR_POLICIES}, got {self.error_policy!r}")
        if self.timeout is not None and float(self.timeout) <= 0:
            raise ValueError("timeout must be positive")
        if self.max_concurrency is not None and int(self.max_concurrency) < 1:
            raise ValueError("max_concurrency must be >= 1")
        # validate the width eagerly so a bad policy fails at construction time,
        # not deep inside a batch run
        resolve_concurrency(self.concurrency, maximum=self.max_concurrency)

    def with_(self, **changes) -> "SchedulerPolicy":
        """Return a copy with the given fields replaced."""
        return replace(self, **changes)

    def width(self, task_count: Optional[int] = None) -> int:
        """Resolved worker count for a batch of ``task_count`` items."""
        if self.backend == BACKEND_SERIAL:
            return 1
        return resolve_concurrency(
            self.concurrency, task_count=task_count, maximum=self.max_concurrency
        )


def default_policy() -> SchedulerPolicy:
    """Policy implied by the environment (``SMART_SLICE_SCHEDULER_*``)."""
    backend = str(setting(ENV_BACKEND, BACKEND_THREAD) or BACKEND_THREAD).strip().lower()
    if backend not in BACKENDS:
        _log.warning("ignoring %s=%r (expected one of %s)", ENV_BACKEND, backend, ", ".join(BACKENDS))
        backend = BACKEND_THREAD
    return SchedulerPolicy(backend=backend, pin_cores=_env_flag(ENV_PIN_CORES, False))


_CURRENT_POLICY: contextvars.ContextVar[Optional[SchedulerPolicy]] = contextvars.ContextVar(
    "smart_slice_scheduler_policy", default=None
)


def current_policy() -> SchedulerPolicy:
    """Policy in effect for the current call, or :func:`default_policy`."""
    return _CURRENT_POLICY.get() or default_policy()


@contextmanager
def use_policy(policy: Optional[SchedulerPolicy]) -> Iterator[SchedulerPolicy]:
    """Publish ``policy`` for the duration of the block (contextvar-scoped).

    Mirrors :func:`smart_slice.options.use_options`: concurrent batches with
    different policies cannot see each other's settings.
    """
    resolved = policy or default_policy()
    token = _CURRENT_POLICY.set(resolved)
    try:
        yield resolved
    finally:
        _CURRENT_POLICY.reset(token)


def resolve_policy(
    policy: Optional[SchedulerPolicy] = None,
    *,
    concurrency: Union[None, int, str] = None,
    backend: Optional[str] = None,
    pin_cores: Optional[bool] = None,
    error_policy: Optional[str] = None,
    ordered: Optional[bool] = None,
    timeout: Optional[float] = None,
    max_concurrency: Optional[int] = None,
    **changes: Any,
) -> SchedulerPolicy:
    """Merge explicit keywords over ``policy`` (or the ambient default).

    Precedence: explicit kwargs > the passed ``policy`` > the contextvar >
    the environment.  This is what lets ``slice_many(files, concurrency=4)``
    and ``slice_many(files, policy=my_policy)`` both work.
    """
    base = policy or current_policy()
    fields: Dict[str, Any] = {}
    if concurrency is not None:
        fields["concurrency"] = concurrency
    if backend is not None:
        fields["backend"] = backend
    if pin_cores is not None:
        fields["pin_cores"] = pin_cores
    if error_policy is not None:
        fields["error_policy"] = error_policy
    if ordered is not None:
        fields["ordered"] = ordered
    if timeout is not None:
        fields["timeout"] = timeout
    if max_concurrency is not None:
        fields["max_concurrency"] = max_concurrency
    fields.update(changes)
    return base.with_(**fields) if fields else base

# --------------------------------------------------------------------------- #
# core pinning ("分配核"): bind a task to one core of the affinity mask
# --------------------------------------------------------------------------- #
# The source platform pinned its sandboxed workers with sched_setaffinity and
# documented Windows/macOS as unsupported.  A library has no such excuse: the
# same round-robin allocation is implemented here on top of sched_setaffinity
# (Linux/BSD) and SetThreadAffinityMask (Windows), and degrades to a logged
# no-op where neither exists (macOS).
_PIN_UNSUPPORTED_WARNED = False

#: Windows affinity masks are DWORD_PTR bitmasks, so this covers one processor
#: group (64 logical processors).  Larger hosts report the first group only.
_WINDOWS_MASK_BITS = 64


def _sched_mask() -> Optional[List[int]]:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if get_affinity is None:
        return None
    try:
        return sorted(get_affinity(0))
    except OSError:  # pragma: no cover - unusual kernel/permission state
        return None


_kernel32 = None
_kernel32_lock = threading.Lock()


def _windows_kernel():
    """``kernel32`` with correct prototypes, resolved once.

    The declarations are not cosmetic.  ``ctypes`` defaults every ``restype`` to
    a 32-bit ``c_int``, which silently truncates the ``HANDLE`` pseudo-handles
    returned by ``GetCurrentProcess``/``GetCurrentThread`` on 64-bit Windows -
    the call then fails and, without this, core pinning quietly degrades to a
    no-op.  ``DWORD_PTR`` is pointer-sized, hence ``c_size_t``.
    """
    global _kernel32
    if _kernel32 is not None:
        return _kernel32 or None
    with _kernel32_lock:
        if _kernel32 is not None:
            return _kernel32 or None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentThread.restype = wintypes.HANDLE
            kernel32.GetProcessAffinityMask.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
            kernel32.SetThreadAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
            kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t
            _kernel32 = kernel32
        except Exception as error:  # noqa: BLE001 - affinity is an optimisation, never fatal
            _log.debug("kernel32 affinity prototypes unavailable: %s", error)
            _kernel32 = False
    return _kernel32 or None


def _windows_mask() -> Optional[List[int]]:
    if os.name != "nt":
        return None
    kernel32 = _windows_kernel()
    if kernel32 is None:
        return None
    try:
        import ctypes

        process_mask = ctypes.c_size_t(0)
        system_mask = ctypes.c_size_t(0)
        ok = kernel32.GetProcessAffinityMask(
            kernel32.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask)
        )
        if not ok:
            return None
        value = int(process_mask.value)
    except Exception as error:  # noqa: BLE001 - see _windows_kernel
        _log.debug("GetProcessAffinityMask failed: %s", error)
        return None
    return [bit for bit in range(_WINDOWS_MASK_BITS) if value >> bit & 1]


def _affinity_mask() -> List[int]:
    """Sorted core ids this process may run on (``[]`` where unsupported)."""
    mask = _sched_mask()
    if mask is None:
        mask = _windows_mask()
    return mask or []


def _windows_pin(core: int):
    if os.name != "nt":
        return None
    kernel32 = _windows_kernel()
    if kernel32 is None:
        return None
    try:
        import ctypes

        previous = kernel32.SetThreadAffinityMask(kernel32.GetCurrentThread(), ctypes.c_size_t(1 << core))
    except Exception as error:  # noqa: BLE001 - see _windows_kernel
        _log.debug("SetThreadAffinityMask failed: %s", error)
        return None
    # 0 means failure; a valid previous mask is never 0
    if not previous:
        return None
    return ("win", int(previous))


def _pin_to_core(core: int):
    """Restrict the calling thread to ``core``; return an opaque restore token.

    ``None`` means pinning is unavailable (unsupported platform, or the core is
    no longer in the process mask) and the caller must not restore anything.
    """
    if core is None or core < 0:
        return None
    set_affinity = getattr(os, "sched_setaffinity", None)
    get_affinity = getattr(os, "sched_getaffinity", None)
    if set_affinity is not None and get_affinity is not None:
        try:
            allowed = get_affinity(0)
        except OSError:  # pragma: no cover - defensive
            return None
        if core not in allowed:
            return None
        try:
            set_affinity(0, {core})
        except OSError as error:  # pragma: no cover - permission/kernel limits
            _log.warning("core pinning failed for core %s: %s", core, error)
            return None
        return ("sched", set(allowed))
    token = _windows_pin(core)
    if token is not None:
        return token
    global _PIN_UNSUPPORTED_WARNED
    if not _PIN_UNSUPPORTED_WARNED:
        _PIN_UNSUPPORTED_WARNED = True
        _log.info("pin_cores is not supported on this platform; running unpinned")
    return None


def _restore_affinity(token) -> None:
    """Undo :func:`_pin_to_core`; a no-op when pinning never happened."""
    if not token:
        return
    kind, value = token
    if kind == "sched":
        set_affinity = getattr(os, "sched_setaffinity", None)
        if set_affinity is None:  # pragma: no cover - guarded by _pin_to_core
            return
        try:
            set_affinity(0, value)
        except OSError as error:  # pragma: no cover - defensive
            _log.warning("restoring the CPU affinity mask failed: %s", error)
        return
    kernel32 = _windows_kernel()
    if kernel32 is None:
        return
    try:
        import ctypes

        kernel32.SetThreadAffinityMask(kernel32.GetCurrentThread(), ctypes.c_size_t(value))
    except Exception as error:  # noqa: BLE001 - defensive
        _log.warning("restoring the thread affinity mask failed: %s", error)


def core_for_index(index: int, cores: Optional[Sequence[int]] = None) -> Optional[int]:
    """Round-robin core allocation: task ``index`` runs on ``cores[index % n]``.

    Deterministic rather than sampled, so a batch is reproducible and tasks
    spread evenly instead of colliding on one core.
    """
    available = list(cores) if cores is not None else _affinity_mask()
    if not available:
        return None
    return available[index % len(available)]

# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class TaskOutcome:
    """One task's result, plus the scheduling facts worth knowing about it.

    :param index:   position in the input sequence (stable under ``ordered=False``)
    :param name:    human readable label for logs (the file name, when slicing)
    :param ok:      the worker returned without raising
    :param value:   the worker's return value (``None`` on failure)
    :param error:   the exception the worker raised (``None`` on success)
    :param elapsed: wall-clock seconds spent inside the worker
    :param worker:  thread or process name that ran it
    :param core:    core id the task was pinned to, when ``pin_cores`` is on
    """

    index: int
    name: str = ""
    ok: bool = True
    value: Any = None
    error: Optional[BaseException] = None
    elapsed: float = 0.0
    worker: str = ""
    core: Optional[int] = None

    def raise_for_status(self) -> Any:
        """Re-raise the task's error, else return its value."""
        if self.error is not None:
            raise self.error
        return self.value


def _format_failure(outcome: TaskOutcome) -> str:
    error = outcome.error
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ) if error is not None and error.__traceback__ is not None else str(error)
    return f"task {outcome.index} ({outcome.name or '<unnamed>'}) failed: {error}; {detail}"


# --------------------------------------------------------------------------- #
# the scheduler
# --------------------------------------------------------------------------- #
def run_parallel(
    items: Sequence[Any],
    worker: Callable[[Any], Any],
    policy: Optional[SchedulerPolicy] = None,
    *,
    name_of: Optional[Callable[[Any], str]] = None,
    concurrency: Union[None, int, str] = None,
    backend: Optional[str] = None,
    pin_cores: Optional[bool] = None,
    error_policy: Optional[str] = None,
    ordered: Optional[bool] = None,
    timeout: Optional[float] = None,
    **changes: Any,
) -> List[TaskOutcome]:
    """Run ``worker`` over ``items`` under a scheduling policy.

    :param items:     the work sequence; results are keyed by position in it
    :param worker:    ``item -> value``; called once per item
    :param policy:    a :class:`SchedulerPolicy`; the keyword arguments below
                      override the matching field
    :param name_of:   ``item -> label`` for log lines (default ``item.name`` or ``str(item)``)
    :returns:         one :class:`TaskOutcome` per item

    Semantics (identical whether the batch ran serially or in a pool):

    * every item is attempted - one failure never cancels the rest of the batch;
    * outcomes come back in input order when ``ordered`` is True, otherwise in
      completion order (each still carries its input ``index``);
    * with ``error_policy="raise_first"`` the error belonging to the lowest
      failing index is re-raised as the original object once the batch has
      drained, and every other failure is logged first so nothing is silently
      swallowed; ``"collect"`` returns them all instead.

    ``backend="process"`` needs ``worker`` and every item to be picklable (no
    closures, no callbacks); a pickling failure is reported as that task's
    error rather than escaping the call.
    """
    resolved = resolve_policy(
        policy,
        concurrency=concurrency,
        backend=backend,
        pin_cores=pin_cores,
        error_policy=error_policy,
        ordered=ordered,
        timeout=timeout,
        **changes,
    )
    total = len(items)
    if total == 0:
        return []

    label = name_of or _default_label
    width = resolved.width(total)
    cores = _affinity_mask() if resolved.pin_cores else []
    use_pool = width > 1 and total > 1 and resolved.backend != BACKEND_SERIAL

    if not use_pool:
        outcomes = [
            _run_inline(index, item, worker, resolved, label)
            for index, item in enumerate(items)
        ]
        return _apply_error_policy(outcomes, resolved)

    by_index: Dict[int, TaskOutcome] = {}
    completion_order: List[TaskOutcome] = []
    executor = _make_executor(resolved, width)
    try:
        futures = {
            executor.submit(_run_task, index, item, worker, resolved, cores, label): index
            for index, item in enumerate(items)
        }
        pending: Iterable[Any] = (
            sorted(futures, key=lambda future: futures[future])
            if resolved.ordered
            else as_completed(futures)
        )
        for future in pending:
            index = futures[future]
            try:
                outcome = future.result(timeout=resolved.timeout)
            except FuturesTimeoutError as error:
                outcome = TaskOutcome(
                    index=index, name=label(items[index]), ok=False, error=error,
                    elapsed=float(resolved.timeout or 0.0),
                )
            except BaseException as error:  # noqa: BLE001 - pool-level failure (pickling, worker death)
                outcome = TaskOutcome(index=index, name=label(items[index]), ok=False, error=error)
            by_index[index] = outcome
            completion_order.append(outcome)
    finally:
        # A timed-out task is abandoned rather than waited for; with processes
        # the pool is torn down so no child keeps burning CPU on stale work.
        executor.shutdown(wait=resolved.backend != BACKEND_PROCESS, cancel_futures=True)

    selected = (
        [by_index[index] for index in sorted(by_index)] if resolved.ordered else completion_order
    )
    return _apply_error_policy(selected, resolved)


def _default_label(item: Any) -> str:
    return str(getattr(item, "name", item))


def _make_executor(policy: SchedulerPolicy, width: int):
    if policy.backend == BACKEND_PROCESS:
        return ProcessPoolExecutor(max_workers=width, mp_context=_process_context())
    return ThreadPoolExecutor(
        max_workers=width, thread_name_prefix=policy.thread_name_prefix
    )


def _process_context():
    """Multiprocessing context for the process backend.

    ``spawn`` everywhere: ``fork`` would copy whatever state the host process
    holds (locks, sockets, a half-initialised OCR engine) into the worker, and
    is unavailable on Windows/macOS by default.  A fresh interpreter costs
    startup time but has no inherited-state failure modes.
    """
    import multiprocessing

    return multiprocessing.get_context("spawn")


def _run_inline(index, item, worker, policy: SchedulerPolicy, label) -> TaskOutcome:
    """Serial path: no pool, no pinning, failures captured exactly as in a pool."""
    name = label(item)
    started = time.perf_counter()
    try:
        if policy.task_setup is not None:
            policy.task_setup()
        try:
            value = worker(item)
        except BaseException as error:  # noqa: BLE001 - captured into the outcome
            return TaskOutcome(
                index=index, name=name, ok=False, error=error,
                elapsed=time.perf_counter() - started, worker="main",
            )
        return TaskOutcome(
            index=index, name=name, ok=True, value=value,
            elapsed=time.perf_counter() - started, worker="main",
        )
    finally:
        if policy.task_teardown is not None:
            policy.task_teardown()


def _run_task(index, item, worker, policy: SchedulerPolicy, cores, label) -> TaskOutcome:
    """Worker body: core pinning, setup/teardown hooks, timing, error capture.

    Runs inside the pool thread (or child process).  ``BaseException`` is caught
    deliberately: an interrupt in one task must not leave its outcome slot
    empty, which would stall result collection for the whole batch.
    """
    pinned: Optional[int] = None
    previous_mask: Optional[set] = None
    if cores:
        candidate = cores[index % len(cores)]
        previous_mask = _pin_to_core(candidate)
        if previous_mask is not None:
            pinned = candidate

    name = label(item)
    started = time.perf_counter()
    try:
        if policy.task_setup is not None:
            policy.task_setup()
        try:
            value = worker(item)
        except BaseException as error:  # noqa: BLE001 - see docstring
            return TaskOutcome(
                index=index, name=name, ok=False, error=error,
                elapsed=time.perf_counter() - started,
                worker=threading.current_thread().name, core=pinned,
            )
        return TaskOutcome(
            index=index, name=name, ok=True, value=value,
            elapsed=time.perf_counter() - started,
            worker=threading.current_thread().name, core=pinned,
        )
    finally:
        try:
            if policy.task_teardown is not None:
                policy.task_teardown()
        finally:
            _restore_affinity(previous_mask)


def _apply_error_policy(outcomes: List[TaskOutcome], policy: SchedulerPolicy) -> List[TaskOutcome]:
    """Ported failure semantics: log every failure, then raise the first by index."""
    failures = [outcome for outcome in outcomes if not outcome.ok]
    if not failures or policy.error_policy == ERROR_COLLECT:
        return outcomes
    failures.sort(key=lambda outcome: outcome.index)
    for outcome in failures[1:]:
        _log.error(_format_failure(outcome))
    raise failures[0].error

# --------------------------------------------------------------------------- #
# batch slicing on top of the scheduler
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SliceJob:
    """One document in a batch.

    :param name:    file name - decides handler dispatch and the title fallback
    :param content: document bytes; when ``None`` the document is read from
                    ``path`` inside the worker (so a batch of large files never
                    holds every document in memory at once)
    :param path:    filesystem path to read when ``content`` is ``None``
    :param kwargs:  per-document overrides for :func:`smart_slice.slice_bytes`
                    (``limit``, ``overlap``, ``options``, ``save_image``, ...).
                    Keys not present here fall back to the batch-wide keywords
                    given to :func:`slice_many`.

    Instances must stay picklable for ``backend="process"``: bytes, str and
    plain dicts are fine, a ``save_image`` closure is not (use threads, or keep
    the callback out of the job).
    """

    name: str
    content: Optional[bytes] = None
    path: Optional[str] = None
    kwargs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.content is None and self.path is None:
            raise ValueError(f"SliceJob({self.name!r}) needs either content or path")

    @classmethod
    def from_path(cls, path: Union[str, "os.PathLike[str]"], name: Optional[str] = None, **kwargs: Any) -> "SliceJob":
        """A job that reads ``path`` inside the worker."""
        resolved = os.fspath(path)
        return cls(name=name or os.path.basename(resolved), path=resolved, kwargs=dict(kwargs))

    @classmethod
    def from_bytes(cls, content: bytes, name: str, **kwargs: Any) -> "SliceJob":
        """A job carrying its bytes (upload-style input)."""
        return cls(name=name, content=bytes(content), kwargs=dict(kwargs))


def as_slice_jobs(inputs: Iterable[Any]) -> List[SliceJob]:
    """Normalise a heterogeneous batch into :class:`SliceJob` objects.

    Accepted per item, mirroring the input shapes the source platform's upload
    and import paths handed its scheduler:

    * :class:`SliceJob` - used as is;
    * ``str`` / ``os.PathLike`` - read from disk inside the worker;
    * ``(name, bytes)`` - an in-memory document;
    * an object with ``.name`` and ``.read()`` (a file object, an upload handle)
      - read once, here, in the calling thread;
    * a ``Mapping`` with ``name`` plus ``content`` and/or ``path``.

    Anything else raises :class:`TypeError` naming the offending item, so a
    batch never fails halfway through with an opaque attribute error.
    """
    jobs: List[SliceJob] = []
    for position, item in enumerate(inputs):
        jobs.append(_as_job(item, position))
    return jobs


def _as_job(item: Any, position: int) -> SliceJob:
    if isinstance(item, SliceJob):
        return item
    if isinstance(item, (str, os.PathLike)):
        return SliceJob.from_path(item)
    if isinstance(item, (bytes, bytearray)):
        raise TypeError(
            f"batch item {position} is raw bytes with no file name; "
            f"pass (name, content) or SliceJob.from_bytes(content, name)"
        )
    if isinstance(item, (tuple, list)) and len(item) == 2:
        name, content = item
        if isinstance(content, (bytes, bytearray)) and isinstance(name, str):
            return SliceJob.from_bytes(content, name)
    if isinstance(item, dict):
        name = item.get("name")
        if not isinstance(name, str):
            raise TypeError(f"batch item {position} is a mapping without a string 'name'")
        content = item.get("content")
        path = item.get("path")
        kwargs = {key: value for key, value in item.items() if key not in ("name", "content", "path")}
        if content is not None:
            return SliceJob.from_bytes(content, name, **kwargs)
        if path is not None:
            return SliceJob.from_path(path, name=name, **kwargs)
        raise TypeError(f"batch item {position} ({name!r}) has neither 'content' nor 'path'")
    name = getattr(item, "name", None)
    read = getattr(item, "read", None)
    if isinstance(name, str) and callable(read):
        return SliceJob.from_bytes(read(), name)
    raise TypeError(
        f"batch item {position} ({type(item).__name__}) is not a path, (name, bytes), "
        f"mapping, file-like object or SliceJob"
    )


#: private slice_kwarg carrying the batch-wide ImageCollector (see slice_many)
_COLLECTOR_KEY = "_collector"


def _slice_one(job: SliceJob, defaults: Optional[Dict[str, Any]] = None) -> Any:
    """Slice a single job.  Module level (not a closure) so it stays picklable."""
    kwargs: Dict[str, Any] = dict(defaults or {})
    kwargs.update(job.kwargs)
    collector = kwargs.pop(_COLLECTOR_KEY, None)
    if job.content is not None:
        content = job.content
    else:
        with open(job.path, "rb") as handle:
            content = handle.read()
    if collector is not None:
        # multimodal path: handlers rarely emit pictures on their own (a
        # standalone .png or an .html never does), so the job goes through
        # slice_multimodal, which also recovers what the handlers missed.
        from smart_slice import slice_multimodal

        return slice_multimodal(content, job.name, collector=collector, **kwargs).paragraphs
    from smart_slice import slice_bytes

    return slice_bytes(content, job.name, **kwargs)


@dataclass
class BatchReport:
    """What a batch run did: results, failures and the scheduling facts.

    :param outcomes: one :class:`TaskOutcome` per input, in input order
    :param policy:   the resolved policy the batch actually ran under
    :param width:    worker count used
    :param elapsed:  wall-clock seconds for the whole batch
    """

    outcomes: List[TaskOutcome] = field(default_factory=list)
    policy: Optional[SchedulerPolicy] = None
    width: int = 1
    elapsed: float = 0.0
    #: every image the batch produced, when ``slice_many(..., collect_images=True)``
    #: installed a collector.  Batch-wide and unordered with respect to documents -
    #: per-document attribution needs :func:`smart_slice.slice_multimodal`.
    images: List[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every document sliced successfully."""
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def failures(self) -> List[TaskOutcome]:
        """Failed outcomes, in input order."""
        return [outcome for outcome in self.outcomes if not outcome.ok]

    @property
    def results(self) -> List[Any]:
        """Per-document results in input order (``None`` where it failed)."""
        ordered = sorted(self.outcomes, key=lambda outcome: outcome.index)
        return [outcome.value for outcome in ordered]

    @property
    def paragraphs(self) -> List[Any]:
        """Every document's paragraphs concatenated in input order.

        This is the flattening the source platform applied to its parallel
        upload results, so a batch can be handed straight to an index builder.
        """
        flat: List[Any] = []
        for result in self.results:
            if isinstance(result, list):
                flat.extend(result)
            elif result is not None:
                flat.append(result)
        return flat

    def raise_first(self) -> None:
        """Raise the lowest-index failure, after logging the others."""
        failures = sorted(self.failures, key=lambda outcome: outcome.index)
        if not failures:
            return
        for outcome in failures[1:]:
            _log.error(_format_failure(outcome))
        raise failures[0].error

    def __len__(self) -> int:
        return len(self.outcomes)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.results)

    def __getitem__(self, index: int) -> Any:
        return self.results[index]

    def summary(self) -> str:
        """One-line human readable report (used by the CLI's ``--stats``)."""
        done = len(self.outcomes) - len(self.failures)
        text = (
            f"{done}/{len(self.outcomes)} documents sliced in {self.elapsed * 1000:.0f} ms "
            f"(width={self.width}, backend={self.policy.backend if self.policy else '?'}, "
            f"pin_cores={bool(self.policy and self.policy.pin_cores)})"
        )
        if self.images:
            text += f", {len(self.images)} images"
        return text


def slice_many(
    inputs: Iterable[Any],
    *,
    policy: Optional[SchedulerPolicy] = None,
    concurrency: Union[None, int, str] = None,
    backend: Optional[str] = None,
    pin_cores: Optional[bool] = None,
    error_policy: Optional[str] = None,
    ordered: Optional[bool] = None,
    timeout: Optional[float] = None,
    max_concurrency: Optional[int] = None,
    task_setup: Optional[Callable[[], None]] = None,
    task_teardown: Optional[Callable[[], None]] = None,
    collect_images: bool = False,
    dedupe_images: bool = True,
    max_images: Optional[int] = None,
    **slice_kwargs: Any,
) -> BatchReport:
    """Slice a batch of documents under a scheduling policy.

    :param inputs:        anything :func:`as_slice_jobs` accepts - paths,
                          ``(name, bytes)`` pairs, file-like uploads, mappings
                          or ready-made :class:`SliceJob` objects
    :param concurrency:   batch width: an int, or ``"auto"`` (default) /
                          ``"cores"`` / ``"serial"`` / ``"2x"``
    :param backend:       ``"thread"`` (default), ``"process"`` or ``"serial"``
    :param pin_cores:     allocate one core per task, round-robin
    :param error_policy:  ``"raise_first"`` (default: whole batch fails, the
                          lowest-index error is raised, the rest are logged) or
                          ``"collect"`` (return everything, inspect
                          ``report.failures``)
    :param ordered:       results in input order (default) or completion order
    :param timeout:       per-document wall-clock seconds
    :param max_concurrency: hard ceiling for a derived width
    :param task_setup:    run inside the worker before each document;
                          ``task_teardown`` runs after it in a ``finally``
    :param slice_kwargs:  passed to every :func:`smart_slice.slice_bytes` call
                          (``limit``, ``overlap``, ``options``, ``patterns``,
                          ``with_filter``, ``normalize``, ``save_image``,
                          ``image_text_extractor``, ``fallback_title``,
                          ``progress_hook``); a job's own ``kwargs`` win
    :returns:             a :class:`BatchReport`

    Chunking options travel correctly across workers: ``slice_bytes`` publishes
    them on a contextvar *inside* the worker, so a thread pool cannot leak one
    document's ``overlap`` into another's.

    ``save_image`` and ``progress_hook`` are shared by every worker - they must
    be thread-safe (a lock around your own list is enough).  They also make the
    batch unpicklable, so ``backend="process"`` needs them left out.

    :param collect_images: route every job through
                           :func:`smart_slice.slice_multimodal` with one shared
                           :class:`~smart_slice.multimodal.ImageCollector` and
                           publish what it caught on ``report.images`` (default
                           False).  The paragraphs themselves are unchanged, so
                           the report reads exactly as before.  An explicit
                           ``save_image=`` still wins and keeps the plain
                           ``slice_bytes`` path.  Because the collector holds a
                           lock, it forces ``backend="thread"`` or ``"serial"`` -
                           a process pool cannot pickle it, so asking for both
                           raises instead of silently returning an empty list.
    :param dedupe_images:  collapse content-identical images across the whole
                           batch onto one id (default True)
    :param max_images:     batch-wide ceiling on collected images (``None`` =
                           :data:`~smart_slice.multimodal.DEFAULT_MAX_IMAGES`)
    """
    jobs = as_slice_jobs(inputs)
    slice_kwargs = dict(slice_kwargs)
    collector = None
    if collect_images and slice_kwargs.get("save_image") is None:
        from .multimodal import DEFAULT_MAX_IMAGES, ImageCollector

        if backend == BACKEND_PROCESS or (
            isinstance(policy, SchedulerPolicy) and policy.backend == BACKEND_PROCESS
        ):
            raise ValueError(
                "collect_images=True needs a thread or serial backend: "
                "an ImageCollector holds a lock and cannot be pickled"
            )
        collector = ImageCollector(
            dedupe=dedupe_images,
            max_images=DEFAULT_MAX_IMAGES if max_images is None else max_images,
        )
        slice_kwargs[_COLLECTOR_KEY] = collector
    if "options" not in slice_kwargs:
        # A worker thread starts with an *empty* context, so the ambient
        # ChunkingOptions published by an enclosing ``use_options(...)`` block
        # would silently not reach it.  Capture it here and pass it explicitly;
        # an explicit ``options=``/``limit=``/``overlap=`` keyword still wins,
        # because resolve_options merges keywords over the object.
        from .options import DEFAULT_OPTIONS, current_options

        ambient = current_options()
        if ambient is not DEFAULT_OPTIONS:
            slice_kwargs["options"] = ambient
    resolved = resolve_policy(
        policy,
        concurrency=concurrency,
        backend=backend,
        pin_cores=pin_cores,
        error_policy=error_policy,
        ordered=ordered,
        timeout=timeout,
        max_concurrency=max_concurrency,
        task_setup=task_setup,
        task_teardown=task_teardown,
    )
    if not jobs:
        return BatchReport(outcomes=[], policy=resolved, width=1, elapsed=0.0,
                           images=collector.assets if collector is not None else [])

    worker = _job_worker(slice_kwargs)
    width = resolved.width(len(jobs))
    started = time.perf_counter()
    outcomes = run_parallel(jobs, worker, resolved)
    elapsed = time.perf_counter() - started
    return BatchReport(
        outcomes=outcomes, policy=resolved, width=width, elapsed=elapsed,
        images=collector.assets if collector is not None else [],
    )


def _job_worker(slice_kwargs: Dict[str, Any]):
    """Bind the batch-wide slicing keywords onto the job worker.

    ``functools.partial`` rather than a closure: a lambda is not picklable, so
    it would break ``backend="process"`` for every batch that passes slicing
    keywords (which is nearly all of them).
    """
    if not slice_kwargs:
        return _slice_one_no_defaults
    return functools.partial(_slice_one, defaults=slice_kwargs)


def _slice_one_no_defaults(job: SliceJob) -> Any:
    return _slice_one(job)


def slice_paths(
    paths: Iterable[Union[str, "os.PathLike[str]"]], **kwargs: Any
) -> BatchReport:
    """Convenience wrapper: :func:`slice_many` over filesystem paths."""
    return slice_many([SliceJob.from_path(path) for path in paths], **kwargs)