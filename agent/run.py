"""Orchestrator: python -m agent.run [--force] [--no-llm] [--demo-heal] [--set load.users=50]

pull -> (build+start) -> sanity -> [self-heal] -> load -> analyze -> logs -> correlate -> report -> publish
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

from .config import ROOT, load_config
from .context import RunContext
from .llm import LLM
from .memory import Memory
from .skills import analyze as analyze_skill
from .skills import correlate, git_sync, heal, logs as logs_skill, publish, report, smoke
from .skills.app_env import App
from .skills.load_test import run_load


def _prepare_locust_dir(ctx: RunContext, source: Path) -> Path:
    d = ctx.out / "locust"
    d.mkdir(exist_ok=True)
    shutil.copy(ROOT / "locust" / "recorder.py", d / "recorder.py")
    shutil.copy(source, d / "locustfile.py")
    return d / "locustfile.py"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agentic performance-testing pipeline")
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", action="append", default=[], metavar="section.key=value")
    ap.add_argument("--force", action="store_true", help="run even if the target commit was already tested")
    ap.add_argument("--no-llm", action="store_true", help="deterministic mode: no model calls at all")
    ap.add_argument("--demo-heal", action="store_true", help="start from a stale Locust script to demonstrate self-healing")
    args = ap.parse_args(argv)

    def _terminate(*_):                                       # CI cancel / kill: still stop the app and write the report
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError):                              # not in the main thread (e.g. demo/tests)
        pass

    cfg = load_config(args.config, args.set)
    # PERF_HOME relocates all mutable state (memory, reports, site, out, target clone) - used for tests and local experiments
    HOME = Path(os.environ.get("PERF_HOME") or ROOT)
    HOME.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(cfg, HOME)
    mem = Memory(HOME)
    llm = LLM(cfg, ctx, enabled=not args.no_llm)
    maven_repo = Path(os.environ["PERF_MAVEN_REPO"]) if os.environ.get("PERF_MAVEN_REPO") else None
    t_run = time.time()
    state: dict = {"heal": None, "analysis": None, "logs": None, "findings": [], "improvements": [], "summary": "",
                   "smoke": {"passed": False, "requests": 0, "failures": 0, "error_rate": 1, "p95_ms": 0, "reasons": [], "top_failures": []}}
    skipped: list[dict] = []
    app = None
    git: dict = {}
    exit_code = 0
    stats = mem.token_stats()

    try:
        # 1 ---- pull the latest code ---------------------------------------------------------------------
        with ctx.step("pull", "Pull latest code") as ev:
            git = git_sync.sync(cfg, HOME, mem.last_sha())
            ev["detail"] = f"{git['short']} {git['subject'][:60]}" + ("" if git["changed"] else " (unchanged)")
        if not git["changed"] and not args.force:
            ctx.skip("sanity", "Sanity (smoke) test", "no new commits since the last tested version")
            print(f"\nNo new commits since {git['short']} - nothing to test (this run cost 0 tokens). Use --force to override.")
            return 0

        # 2 ---- build + start the app --------------------------------------------------------------------
        app = App(cfg, git["workdir"], ctx.out, maven_repo)
        with ctx.step("env", "Build and start the application") as ev:
            b = app.build()
            s = app.start()
            ev["detail"] = f"build {b}s, ready after {s}s"

        # which Locust script do we start from?
        if args.demo_heal:
            source, origin = ROOT / "locust" / "broken" / "locustfile.py", "stale demo script"
        elif mem.good_locustfile():
            (ctx.out / "good.py").write_text(mem.good_locustfile(), encoding="utf-8")
            source, origin = ctx.out / "good.py", "last known-good script"
        else:
            source, origin = ROOT / "locust" / "locustfile.py", "default script"
        locustfile = _prepare_locust_dir(ctx, source)

        # 3 ---- sanity -------------------------------------------------------------------------------------
        with ctx.step("sanity", "Sanity (smoke) test") as ev:
            state["smoke"] = smoke.run_smoke(cfg, locustfile, ctx.out)
            ev["detail"] = f"{origin}: {state['smoke']['requests']} req, {state['smoke']['failures']} failed, p95 {state['smoke']['p95_ms']} ms"
            if not state["smoke"]["passed"]:
                ev["detail"] += " -> FAILED: " + "; ".join(state["smoke"]["reasons"])
                ev["status"] = "failed"

        # 4 ---- self-heal (only if needed) ----------------------------------------------------------------
        if state["smoke"]["passed"]:
            skipped.append({"step": "heal", "reason": "smoke test passed, no repair needed", "est_tokens_saved": stats.get("heal")})
            ctx.skip("heal", "Self-heal the Locust script", "smoke test passed - no repair needed", "agent")
            if origin != "last known-good script":
                mem.save_good_locustfile(locustfile.read_text(encoding="utf-8"))
        elif llm.enabled:
            with ctx.step("heal", "Self-heal the Locust script", "agent") as ev:
                state["heal"] = heal.heal(cfg, llm, ctx, locustfile, state["smoke"], ctx.out)
                h = state["heal"]
                ev["detail"] = f"{'healed' if h['healed'] else 'NOT healed'} after {h['attempts']} attempt(s)"
                if h["healed"]:
                    locustfile.write_text(h["source"], encoding="utf-8")
                    mem.save_good_locustfile(h["source"])
                    state["smoke"] = {**state["smoke"], "passed": True}
                    state["smoke"]["healed_requests"] = h["history"][-1].get("requests", 0)
                else:
                    ev["status"] = "failed"
        else:
            ctx.skip("heal", "Self-heal the Locust script", "smoke failed but the LLM is disabled (no key or --no-llm)", "agent")

        if not state["smoke"]["passed"]:
            ctx.verdict = "failed"
            state["summary"] = "The sanity test failed and could not be repaired, so the load test was not run. " + "; ".join(state["smoke"]["reasons"])
            exit_code = 2
        else:
            # 5 ---- load test --------------------------------------------------------------------------------
            with ctx.step("load", f"Run the load test ({cfg['load']['users']} users, {cfg['load']['duration_s']} s)") as ev:
                load = run_load(cfg, locustfile, ctx.out, app.java_process)
                ev["detail"] = f"{len(load['records'])} requests, {len(load['metrics'])} resource samples"
            state["load"] = load
    except Exception as exc:  # noqa: BLE001 - the report must still be produced
        traceback.print_exc()
        ctx.verdict = "failed"
        state["summary"] = f"The run aborted: {type(exc).__name__}: {str(exc)[:300]}"
        exit_code = 2
    finally:
        if app:
            app.stop()                                                  # flushes app.log

    load = state.get("load")
    parsed_logs = {"events": [], "exceptions": [], "gc": [], "lines": 0}
    if load and ctx.verdict != "failed":
        try:
            # 6 ---- analyse -------------------------------------------------------------------------------------
            with ctx.step("analyze", "Analyze results") as ev:
                state["analysis"] = analyze_skill.analyze(load["records"], load["metrics"], cfg, mem.baseline(int(cfg["load"]["users"])))
                a = state["analysis"]
                ev["detail"] = (f"p95 {a['overall'].get('p95', 0)} ms, {len(a['spikes'])} spike(s), "
                                f"{len(a['error_clusters'])} error cluster(s), verdict {a['verdict']}")
            # 7 ---- logs --------------------------------------------------------------------------------------------
            with ctx.step("logs", "Inspect application and GC logs") as ev:
                parsed_logs = logs_skill.parse(ctx.out / "app.log")
                t_start, t_end = state["analysis"]["overall"]["t_start"], state["analysis"]["overall"]["t_end"]
                state["logs"] = logs_skill.summarize(parsed_logs, t_start, t_end, pkg=cfg["target"]["app_package"])
                ev["detail"] = f"{state['logs']['log_lines']} lines, {len(state['logs']['exceptions'])} exception type(s), {state['logs']['gc']['count']} GC events"
            # 8 ---- correlate with code ------------------------------------------------------------------------------
            inv = correlate.Investigator(git["workdir"], state["analysis"], parsed_logs, load["metrics"], state["analysis"]["overall"]["t_start"], cfg["target"]["app_package"])
            bundle = analyze_skill.evidence_bundle(state["analysis"], git, state["logs"], mem.known_issues())
            result = None
            if analyze_skill.needs_investigation(state["analysis"]):
                if llm.enabled:
                    with ctx.step("correlate", "Correlate with logs, code and recent commits", "agent") as ev:
                        result = correlate.correlate_agent(llm, inv, bundle)
                        ev["detail"] = "LLM agent with code/log tools" if result else "agent gave no usable answer - using rule-based analysis"
                if result is None:
                    with ctx.step("correlate_rules", "Correlate with logs and code (rule-based)") as ev:
                        result = correlate.correlate_heuristic(inv, bundle, state["analysis"], state["logs"])
                        ev["detail"] = f"{len(result['findings'])} finding(s)"
            else:
                skipped.append({"step": "correlate", "reason": "no errors, regressions or steady-state spikes to investigate", "est_tokens_saved": stats.get("correlate")})
                ctx.skip("correlate", "Correlate with logs, code and recent commits", "nothing anomalous to investigate - no tokens spent", "agent")
                result = correlate.correlate_heuristic(inv, bundle, state["analysis"], state["logs"])
            state["findings"] = mem.record_findings(result["findings"], ctx.run_id)
            state["improvements"], state["summary"] = result["improvements"], result["summary"]
            ctx.verdict = state["analysis"]["verdict"]
            if state["findings"] and ctx.verdict == "healthy":
                ctx.verdict = "attention"
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            ctx.verdict = "failed"
            state["summary"] = f"Analysis failed: {type(exc).__name__}: {str(exc)[:300]}"
            exit_code = 2

    # 9 ---- report + publish -----------------------------------------------------------------------------------------
    try:
        with ctx.step("report", "Build report and publish") as ev:
            R = {
                "run_id": ctx.run_id, "finished_at": publish.now_iso(), "duration_s": round(time.time() - t_run, 1), "verdict": ctx.verdict,
                "commit": {k: git.get(k, d) for k, d in (("sha", "unknown"), ("short", "unknown"), ("subject", "(run aborted before pulling code)"),
                                                          ("date", ""), ("repo", cfg["target"]["repo_url"].removesuffix(".git")), ("changed_files", []))},
                "smoke": state["smoke"], "heal": state["heal"], "analysis": state["analysis"], "logs": state["logs"],
                "findings": state["findings"], "improvements": state["improvements"], "summary": state["summary"],
                "load": {"users": int(cfg["load"]["users"]), "duration_s": int(cfg["load"]["duration_s"])},
                "llm_mode": "off (deterministic)" if not llm.enabled else ("scripted demo (no real model)" if llm.model == "scripted-demo" else llm.model),
                "tokens": {"total": ctx.total_tokens(), "by_step": ctx.tokens, "skipped": skipped},
                "trace": ctx.trace(),
            }
            ev["status"], ev["detail"] = "ok", f"reports/runs/{ctx.run_id}/report.html"
            R["trace"] = ctx.trace()
            ctx.write_trace()
            html = report.build_html(R)
            summary = publish.publish(HOME, cfg, R, html, ctx.out)
        R["trace"] = ctx.trace()
        # persist memory only for runs that actually tested something
        if ctx.verdict != "failed" and state["analysis"] and git:
            mem.save_state(git["sha"], ctx.run_id, ctx.verdict)
            a = state["analysis"]
            steady_spikes = [s for s in a["spikes"] if s["phase"] != "warm-up"]
            saturated = (a["resources"] or {}).get("cpu_saturated_s", 0) >= 10
            if not a["regressions"] and not steady_spikes and not saturated:      # latency is representative (errors are tracked separately)
                mem.add_baseline_run(ctx.run_id, {e["name"]: e["p95"] for e in a["endpoints"]}, users=int(cfg["load"]["users"]))
            mem.update_token_stats(ctx.tokens)
        print(f"\nVerdict: {ctx.verdict}  |  tokens used: {ctx.total_tokens()}  |  report: reports/runs/{ctx.run_id}/report.html")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
