# Test pollution investigation — task #94 — 2026-05-14

**Result: the "62 failures from test pollution" are actually 82 failures (62
FAILED + 20 ERROR) all caused by a single root cause — `CoreFoundation`
`*** multi-threaded process forked ***` SIGSEGV downstream of one specific
test's HTTP fan-out background task. Recommended fix is ~15 LOC in
`backend/tests/test_api_flock.py`.**

This file is the analysis-only deliverable for the surgical fix dispatch that
will land tomorrow. Same shape as `qa/captures/ap0-nm-investigation-2026-05-14.md`
and `qa/captures/rsync-perm-strip-investigation-2026-05-14.md`: investigation +
recommendation, no commit / code change.

## §1 Reproduction

```
cd backend && python -m pytest -q --tb=no
```

Result: **62 failed, 1145 passed, 7 skipped, 17 warnings, 20 errors in 55.56s**

Per-file count (62 + 20 = 82 total):

| File | Failures | Errors |
|------|----------|--------|
| `tests/test_firstboot_oneshot.py` | 21 | — |
| `tests/test_install_sh.py` | 6 | 20 |
| `tests/test_image_scripts.py` | 17 | — |
| `tests/test_tailscale.py` | 6 | — |
| `tests/test_sweep_orphan_chunks.py` | 6 | — |
| `tests/test_demo_build_htaccess.py` | 3 | — |
| `tests/test_api_system.py` | 2 | — |
| `tests/test_bake.py` | 1 | — |

All 8 files PASS in isolation:

```
test_firstboot_oneshot: 22 passed, 1 skipped
test_install_sh: 28 passed
test_image_scripts: 20 passed
test_tailscale: 9 passed
test_sweep_orphan_chunks: 7 passed
test_demo_build_htaccess: 3 passed
test_api_system: 24 passed
test_bake: 1 passed
```

**Test pollution confirmed** — but the mechanism is not what the dispatch
suspected (subprocess fakes / module-level singletons / FastAPI TestClient
state).

## §2 Failure categorization

There is **one** category, not five. All 82 failures share an identical
fingerprint: a child subprocess returns `<Signals.SIGSEGV: 11>` (exit code
-11) with empty stdout and empty stderr. The Python parent does not crash;
only the spawned child does. Representative tracebacks:

**test_install_sh (20 ERROR):** fixture `install_sh_dry_run` invokes
`subprocess.run(['bash', '/.../scripts/install.sh', '--dry-run', ...])`. Result:

```
subprocess.CalledProcessError: Command '['bash', '.../install.sh',
  '--dry-run', '--root', '...']' died with <Signals.SIGSEGV: 11>.
```

**test_firstboot_oneshot (21 FAILED):** `_run_oneshot()` calls
`subprocess.run(['bash', '.../system/openmarquee-firstboot.sh'], ...)`.
Same SIGSEGV.

**test_bake (1 FAILED):** runs `subprocess.run([sys.executable,
'scripts/bake.py', ...])`. `result.returncode == -11`. Python child crashes,
not bash.

**test_tailscale (6 FAILED) / test_api_system (2 FAILED):** no direct
`subprocess.` call in the test, but the prod code path
(`openmarquee.tailscale.start_up`) launches a `tailscale` stub via
`asyncio.create_subprocess_exec`. Same SIGSEGV in the child.

**Conclusion**: the pollution leaves the parent Python process in a state
where any subsequent child subprocess (bash, Python, ls, echo, anything) is
killed with SIGSEGV at spawn time.

## §3 Root cause — CoreFoundation fork-after-thread

Bisection narrowed the polluter to a single test:
`tests/test_api_flock.py::test_full_lifecycle_through_the_api`. But the test
itself is not special — running ANY of the test_api_flock POST/PATCH tests
in isolation reproduces:

```
test_post_adds_a_peer + bake: 1 failed, 1 passed
test_patch_toggles_sync_flag + bake: 1 failed, 1 passed
test_full_lifecycle_through_the_api + bake: 1 failed, 1 passed
```

The common mechanism: every `client` fixture in `test_api_flock.py` enters
the FastAPI `lifespan` (via `with TestClient(app) as test_client:` at
`tests/test_api_flock.py:46`). Then POST `/api/flock` schedules a
`BackgroundTasks` entry calling `FlockSync.gossip_add` /
`FlockSync.probe_peer_name` (`backend/openmarquee/api_flock.py:154-161`).
Both helpers do a real `httpx.AsyncClient` request against the addresses
passed in (`lobby.ts.net`, `cafeteria.ts.net`, etc.) which fail with
`ConnectError` / `ConnectTimeout` — but the *attempt* loads
Apple's CFNetwork stack.

Minimal repro (no FastAPI, no pytest):

```python
import urllib.request
try: urllib.request.urlopen('http://1.2.3.4/', timeout=0.05)
except Exception: pass
import subprocess
print(subprocess.run(['/bin/echo', 'hello'],
                     capture_output=True, text=True).returncode)
# -> -11
```

The macOS crash report at
`~/Library/Logs/DiagnosticReports/Python-2026-05-14-150547.ips` confirms it:

```
"exception": {"signal": "SIGSEGV",
              "subtype": "KERN_INVALID_ADDRESS at 0x000000010873c961"},
"asi": {
  "CoreFoundation": ["*** multi-threaded process forked ***"],
  "libsystem_c.dylib": ["crashed on child side of fork pre-exec"]
}
```

This is the classic macOS **CoreFoundation fork-after-multithread** bug.
Apple's CFNetwork (loaded via `urllib.urlopen` / `httpx.post` / Python's
`asyncio` resolver thread pool) registers `pthread_atfork` handlers that
abort the child if the process was multi-threaded when fork was called. The
parent stays healthy; only children die.

Notably, `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` does **not** fix this —
that env var only disables `objc_class_init` fork-safety, not
CoreFoundation's. Python 3.13's `subprocess._USE_POSIX_SPAWN` is `True` and
`_USE_VFORK` is `True`, but the SIGSEGV still occurs even with `posix_spawn`
on Apple Silicon (Apple's `posix_spawn` implementation runs CoreFoundation
init hooks in the child before exec — that's the crash site).

## §4 Recommended surgical fix

**One-paragraph design (the dispatch goal):**

Override `get_flock_sync` in `backend/tests/test_api_flock.py`'s `client`
and `manifest_client` fixtures so the FlockSync instance constructed for
the FastAPI app uses an `httpx.MockTransport` instead of a real
`httpx.AsyncClient`. The FlockSync class already takes
`http_client_factory` as a constructor argument (see its docstring at
`backend/openmarquee/flock_sync.py:43-47`: "HTTP client construction is
factored behind `http_client_factory` so tests can swap in a MockTransport
without the code under test knowing"). The wiring is in place; the test
fixtures just don't use it. Pattern: build a `MockTransport(lambda req:
httpx.Response(204))` (returns success for every peer ping with no real
network), construct `FlockSync(..., http_client_factory=lambda:
httpx.AsyncClient(transport=mock))`, override `app.dependency_overrides[
get_flock_sync] = lambda: that_sync`, and clear
`_flock_sync_singleton.cache_clear()` in the fixture teardown alongside the
existing `_flock_storage_singleton.cache_clear()`. Estimated 12-15 LOC.

After the fix lands:

1. `gossip_add` / `probe_peer_name` / `announce_sync_to_peer` background
   tasks return 204 instantly from the mock instead of triggering DNS +
   CFNetwork init.
2. CoreFoundation never gets the "multi-threaded process" stamp.
3. Subsequent `subprocess.run` calls in `test_bake.py`,
   `test_firstboot_oneshot.py`, `test_install_sh.py`, etc. fork+exec
   cleanly.
4. All 82 failures disappear.

**Verification gate:** the same dispatch should run
`cd backend && python -m pytest -q` after the fix and assert `1227 passed,
7 skipped` (current 1145 + 82 = 1227).

## §5 No second-priority category — they're all the same

The dispatch anticipated multiple categories (subprocess fakes, fs state,
module singletons, TestClient state, settings.json). Investigation
confirms: **there is exactly one root cause**, and the 82 failures across
8 test files all reduce to it. The dispatch's task #100 / #99 shape holds
— one investigation, one surgical fix.

The fix is bounded to two fixtures (`client`, `manifest_client`) in one
file (`test_api_flock.py`) plus possibly a single-line addition in
`conftest.py` to make the mock factory a session-shared helper.

## Subagent LGTM

Pure-analysis pass: no code changes, no commits. The minimal repro is
deterministic (3 consecutive runs all reproduce SIGSEGV). The Apple crash
report at `~/Library/Logs/DiagnosticReports/Python-2026-05-14-150547.ips`
provides ground-truth signal for the root cause attribution. The
recommended fix uses an existing dependency-injection seam
(`http_client_factory`) that the FlockSync class already exposes — no
production code change, fix is test-only.
