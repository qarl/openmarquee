# HANDOFF: network-supervisor card + renderer crash-loop (2026-09-23)

**Status:** (a) stuck-card FIXED + committed (7a08f22, NOT deployed). (b) renderer crash-loop NOT root-caused — the real remaining bug. Supervisor DISABLED on-device (admin's interim drop-in) so qarl's sign is clean. HEAD 7a08f22 on task/fireplacesign-burn-prep-2026-09-16 (NOT merged).

## Confirmed root (admin's disable-A/B)
The network supervisor is the root of BOTH symptoms. `OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR=1` + clean reboot → 0 DEGRADED cards, 0 renderer deaths, fire smooth ~25.6fps, single stable renderer PID. Enable → DEGRADED "Lost the wifi connection" card ~1s after boot + never clears + (on a backend restart) the renderer respawns every ~13-15s (dies rc=0, watchdog-detected, no panic/OOM). Tonight's freeze fix (6ec9992) changed the supervisor (off-loop recv via to_thread + reply socket moved out of PrivateTmp), which activated the event-driven card path for the first time (before: recv timed out 100%, zero events).

## (a) Stuck DEGRADED card — FIXED (7a08f22), tested, NOT deployed
Root: card-clear is edge-triggered ONLY on a transient wpa CTRL-EVENT-CONNECTED (STA_ASSOCIATED → DEGRADED→LINGER→ONLINE→clear). No level-triggered reconciliation — the STA-freq poll that DOES see recovery (apply_sta_freq/decide_channel_follow, "already_on_target") never calls apply_event. Missed CONNECTED (wpa-socket reconnect gap after reboot) → stuck DEGRADED → ttl=None card persists to the 60-min renderer cap. Fix: observe-loop poll branch (network_supervisor_loop.py ~352) now feeds `apply_event(STA_ASSOCIATED)` when the poll returns a valid freq AND current_state==DEGRADED. Test: test_network_supervisor_loop.py::test_loop_recovers_stuck_degraded_card_via_sta_freq_poll (fail-before/pass-after). Renderer side is a faithful mirror (ipc_main.rs ttl→deadline; hdmi.rs:18229 auto-clear; 60-min cap hdmi.rs:18189) — no renderer change.

## (b) Renderer respawn loop — UNRESOLVED (the real remaining bug)
Trigger: a BACKEND RESTART (not full reboot) with the supervisor ENABLED. It rendered fine after a reboot 17:42-17:56, then `systemctl restart openmarquee-backend` → loop. NOTE: my deploy-to-sign restarts (stop→transfer-delay→start) did NOT loop — only a plain fast restart did (possible timing/DRM-handoff factor). Renderer inits fine (EGL 1.5, V4L2 REQBUFS ok, DecoderInner drop) then clean rc=0 exit ~13s in → watchdog respawn → supervisor re-boots DEGRADED → re-shows card.

Mechanism NOT determined. Hypotheses to test (MEASURE — strace-both-ends + py-spy, the technique that nailed the freeze; NOTE py-spy was removed from /opt venv, reinstall):
1. DEGRADED supervisor CHURN starves the render IPC across restart → renderer watchdog kills it (rc=0). Check: does the supervisor re-render the card / retry connect rapidly on restart, loading the ThreadPoolExecutor so advance() to_thread is starved? (The freeze was loop-starvation; this may be executor-starvation.)
2. The renderer dies rc=0 = stdin EOF (backend closed its pipe) OR a watchdog SIGTERM. Capture the renderer's LAST journal lines before "subprocess died" (I did NOT capture these — the reboot cleared the loop before I could). That distinguishes: no-advance-fed (loop/executor starvation) vs a present/DRM failure (restart DRM-master handoff race).
3. Does the card RenderSystemCard IPC + rotation=90 present interact badly? (rotation is 90.)

Reproduce SAFELY: NOT on qarl's live sign. Use the dev Pi (openmarqueedev, Tailscale) if it has HDMI, OR a coordinated window: re-enable supervisor (remove admin's drop-in) → `systemctl restart openmarquee-backend` → observe the loop → capture renderer death-reason + strace/py-spy the uvicorn main thread + executor → then re-disable/reboot to recover. rotation stays 90.

Watchdog specifics to read: renderer watchdog rust_renderer.py `_watchdog_loop` (~1526) + `notify_watchdog`; systemd WatchdogSec in system/openmarquee-backend.service; what feeds advance (playback.py ~1470, to_thread).

## Interim + do-not
- Supervisor DISABLED via admin's persistent drop-in → qarl's sign clean. DO NOT re-enable until (b) is root-caused + fixed + verified.
- Card fix 7a08f22 NOT deployed. Before deploy: root-cause+fix (b), full backend suite, subagent diff-review of 7a08f22 (skipped at commit under context pressure). Deploying = a backend restart = would currently trigger (b).
- Options to weigh for (b): if it's my-change-specific, consider whether the off-loop/socket change needs adjustment (e.g. dedicated executor for wpa work so it can't starve advance) vs a pre-existing restart-DRM race merely exposed. MEASURE first.

## Session context
See memory: project_fireplacesign_burn_bugs_2026_09_22 (updated), feedback_frozen_glass_smooth_metrics_look_upstream, feedback_overlay_deploy_skips_opt_recipe_tree. ssh: openmarquee@192.168.1.174 key ~/.ssh/id_ed25519_noether (ssh-agent env at the session scratchpad/agent.env). deploy-to-sign at ../qa/deploy-to-sign.sh.
