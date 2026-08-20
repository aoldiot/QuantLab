"""DeepSeek Harness Python SDK capability probe.

Runs a battery of isolated tests against the bundled DSH runtime and reports
pass/fail/skip per capability. Intended to validate whether the official
DeepSeek Harness SDK can serve as the agent engine for QuantLab's
research -> coding -> backtest -> bug-fix workflow.

Config (all optional, read from environment):
    DSH_PROBE_BASE_URL   OpenAI-compatible base URL, e.g.
                         https://api.deepseek.com/v1  (DeepSeek official)
                         http://host:port/v1          (any OpenAI-compat proxy)
                         The adapter appends /chat/completions itself.
    DSH_PROBE_API_KEY    API key / token.
    DSH_PROBE_MODEL      Model id, e.g. deepseek-chat / deepseek-reasoner.
    DSH_PROBE_MAX_TOKENS Optional per-request output cap (default none).
    DSH_PROBE_TIMEOUT    Per-test wall-clock timeout in seconds (default 240).

Without an API key the probe still runs the pre-flight tests (runtime launch,
initialize, error resilience) and prints the exact credential format needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, RunResult

BASE_URL = os.environ.get("DSH_PROBE_BASE_URL", "https://api.deepseek.com/v1").strip()
API_KEY = os.environ.get("DSH_PROBE_API_KEY", "").strip()
MODEL = os.environ.get("DSH_PROBE_MODEL", "deepseek-chat").strip()
MAX_TOKENS = int(os.environ.get("DSH_PROBE_MAX_TOKENS", "") or 0) or None
PER_TEST_TIMEOUT = float(os.environ.get("DSH_PROBE_TIMEOUT", "240"))
OUT_PATH = os.environ.get("DSH_PROBE_OUT", "").strip()

HAS_CREDENTIALS = bool(API_KEY and MODEL)


def log(msg: str) -> None:
    print(msg, flush=True)


def build_harness(workspace: Path, sessions: Path) -> DeepSeekHarness:
    return DeepSeekHarness(
        provider="deepseek-official",
        model=MODEL,
        max_tokens=MAX_TOKENS,
        cwd=str(workspace),
        session_root=str(sessions),
        base_url=BASE_URL,
        api_key=API_KEY,
    )


def run_with_timeout(harness: DeepSeekHarness, session_id: str, prompt: str, on_notification=None):
    """Run a turn in a daemon thread with a wall-clock timeout."""
    result_holder: dict[str, object] = {"result": None, "error": None}

    def _work() -> None:
        try:
            result_holder["result"] = harness.run(prompt, session_id=session_id, on_notification=on_notification)
        except BaseException as exc:  # noqa: BLE001
            result_holder["error"] = exc

    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    thread.join(timeout=PER_TEST_TIMEOUT)
    if thread.is_alive():
        return None, TimeoutError(f"run exceeded {PER_TEST_TIMEOUT}s wall-clock timeout")
    if result_holder["error"] is not None:
        return None, result_holder["error"]
    return result_holder["result"], None


def summarize_notifications(notifications) -> dict[str, object]:
    methods: dict[str, int] = {}
    event_types: dict[str, int] = {}
    samples: list[dict[str, object]] = []
    for notif in notifications:
        methods[notif.method] = methods.get(notif.method, 0) + 1
        if notif.method == "session.event":
            event = notif.payload.get("event")
            if isinstance(event, dict):
                etype = str(event.get("type", "?"))
                event_types[etype] = event_types.get(etype, 0) + 1
                if len(samples) < 3:
                    samples.append({"type": etype, "sample": event})
    return {
        "methods": dict(sorted(methods.items(), key=lambda kv: -kv[1])),
        "event_types": dict(sorted(event_types.items(), key=lambda kv: -kv[1])),
        "samples": samples,
    }


class Probe:
    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def record(self, name: str, status: str, detail: str = "", data: object | None = None) -> None:
        row = {"name": name, "status": status, "detail": detail}
        if data is not None:
            row["data"] = data
        self.results.append(row)
        marker = {"PASS": "PASS ", "WARN": "WARN ", "FAIL": "FAIL ", "SKIP": "SKIP "}[status]
        log(f"[{marker}] {name}: {detail}")

    def make_tmp(self) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="dsh-probe-"))
        return tmp / "workspace", tmp / "sessions"

    def t01_runtime_launch(self) -> None:
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        try:
            harness = build_harness(ws, ss)
            harness.start()
            log(f"[INFO ] runtime launched: provider=deepseek-official model={MODEL} max_tokens={MAX_TOKENS}")
            harness.close()
            self.record("runtime_launch", "PASS", "bundled runtime exe started and initialized")
        except BaseException as exc:  # noqa: BLE001
            harness.close()
            self.record("runtime_launch", "FAIL", f"{type(exc).__name__}: {exc}")

    def t02_plain_reasoning(self) -> None:
        if not HAS_CREDENTIALS:
            self.record("plain_reasoning", "SKIP", "需要 DSH_PROBE_API_KEY 与 DSH_PROBE_MODEL")
            return
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = build_harness(ws, ss)
        result, err = run_with_timeout(harness, "t02", "Reply with exactly this line and nothing else: DSH_PROBE_OK")
        if err is not None:
            self.record("plain_reasoning", "FAIL", f"{type(err).__name__}: {err}")
            harness.close()
            return
        assert isinstance(result, RunResult)
        ok = "DSH_PROBE_OK" in result.final_response
        summary = summarize_notifications(result.notifications)
        data = {
            "final_response": result.final_response[:300],
            "finish_reason": result.finish_reason,
            "events_count": len(result.events),
            "notifications": summary,
        }
        self.record(
            "plain_reasoning",
            "PASS" if ok else "WARN",
            f"final_response={'match' if ok else 'no-match'}, finish_reason={result.finish_reason}, events={len(result.events)}",
            data,
        )
        harness.close()

    def t03_bash_tool(self) -> None:
        if not HAS_CREDENTIALS:
            self.record("bash_tool", "SKIP", "需要 DSH_PROBE_API_KEY 与 DSH_PROBE_MODEL")
            return
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = build_harness(ws, ss)
        prompt = (
            "Use the bash tool to run: `pwd` and `echo DSH_BASH_OK=$((40+2))`.\n"
            "Report the exact command outputs, including DSH_BASH_OK=42."
        )
        result, err = run_with_timeout(harness, "t03", prompt)
        if err is not None:
            self.record("bash_tool", "FAIL", f"{type(err).__name__}: {err}")
            harness.close()
            return
        assert isinstance(result, RunResult)
        summary = summarize_notifications(result.notifications)
        tool_event_types = [t for t in summary["event_types"] if "tool" in t]
        has_bash = len(tool_event_types) > 0
        content_ok = "42" in result.final_response
        self.record(
            "bash_tool",
            "PASS" if (content_ok and has_bash) else ("WARN" if content_ok else "FAIL"),
            f"output_contains_42={content_ok}, tool_event_types={tool_event_types or 'NONE'}, finish_reason={result.finish_reason}",
            {"method_counts": summary["methods"], "event_types": summary["event_types"], "final_response": result.final_response[:400]},
        )
        harness.close()

    def t04_file_io(self) -> None:
        if not HAS_CREDENTIALS:
            self.record("file_io", "SKIP", "需要 DSH_PROBE_API_KEY 与 DSH_PROBE_MODEL")
            return
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = build_harness(ws, ss)
        prompt = (
            "Use bash to write the text `DSH_FILE_OK` into a file named probe.txt in the current "
            "workspace, then read it back with cat. Report what cat printed."
        )
        result, err = run_with_timeout(harness, "t04", prompt)
        file_ok = (ws / "probe.txt").is_file() and "DSH_FILE_OK" in (ws / "probe.txt").read_text(encoding="utf-8", errors="replace")
        if err is not None:
            self.record("file_io", "FAIL", f"{type(err).__name__}: {err}")
        else:
            assert isinstance(result, RunResult)
            self.record(
                "file_io",
                "PASS" if file_ok else "FAIL",
                f"file_written={file_ok}, final_response_has_ok={'DSH_FILE_OK' in result.final_response}, "
                f"workspace_files={[p.name for p in ws.iterdir()] or 'NONE'}",
                {"finish_reason": result.finish_reason},
            )
        harness.close()

    def t05_session_persistence(self) -> None:
        if not HAS_CREDENTIALS:
            self.record("session_env_persistence", "SKIP", "需要 DSH_PROBE_API_KEY 与 DSH_PROBE_MODEL")
            return
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = build_harness(ws, ss)
        r1, err1 = run_with_timeout(harness, "t05", "Run `export DSH_MARKER=persist-9527` with bash and say done.")
        if err1 is not None:
            self.record("session_env_persistence", "FAIL", f"first turn {type(err1).__name__}: {err1}")
            harness.close()
            return
        r2, err2 = run_with_timeout(harness, "t05", "Run `echo $DSH_MARKER` with bash and report the printed value.")
        if err2 is not None:
            self.record("session_env_persistence", "FAIL", f"second turn {type(err2).__name__}: {err2}")
            harness.close()
            return
        assert isinstance(r2, RunResult)
        persisted = "persist-9527" in r2.final_response
        note = (
            "PASS" if persisted else
            "WARN: 默认 bash 工具每次调用为全新 shell（fresh shell），跨轮不保留环境变量；" \
            "若需要持久 shell，须在自定义 cordis 配置中挂载 PTY terminal 插件"
        )
        self.record(
            "session_env_persistence",
            "PASS" if persisted else "WARN",
            f"same_session_bash_env_persisted={persisted}; {note}",
            {"first_finish": r1.finish_reason, "second_final": r2.final_response[:200]},
        )
        harness.close()

    def t06_notification_streaming(self) -> None:
        if not HAS_CREDENTIALS:
            self.record("notification_streaming", "SKIP", "需要 DSH_PROBE_API_KEY 与 DSH_PROBE_MODEL")
            return
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = build_harness(ws, ss)
        received: list[object] = []

        def on_notification(notif) -> None:
            received.append(notif.method)

        result, err = run_with_timeout(
            harness,
            "t06",
            "Run `ls` once with bash, then answer with the word CONFIRM.",
            on_notification=on_notification,
        )
        if err is not None:
            self.record("notification_streaming", "FAIL", f"{type(err).__name__}: {err}")
            harness.close()
            return
        assert isinstance(result, RunResult)
        live = len(received)
        summary = summarize_notifications(result.notifications)
        self.record(
            "notification_streaming",
            "PASS" if live > 0 else "FAIL",
            f"on_notification_calls={live}, events={len(result.events)}",
            summary,
        )
        harness.close()

    def t07_max_tokens_truncation(self) -> None:
        if not HAS_CREDENTIALS:
            self.record("max_tokens_truncation", "SKIP", "需要 DSH_PROBE_API_KEY 与 DSH_PROBE_MODEL")
            return
        if MAX_TOKENS is None:
            self.record("max_tokens_truncation", "SKIP", "未设置 DSH_PROBE_MAX_TOKENS（该测试需要显式限制）")
            return
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = build_harness(ws, ss)
        result, err = run_with_timeout(harness, "t07", "Print the word A repeated 20000 times in one message.")
        if err is not None:
            self.record("max_tokens_truncation", "FAIL", f"{type(err).__name__}: {err}")
            harness.close()
            return
        assert isinstance(result, RunResult)
        truncated = result.finish_reason in ("length", "max-tokens", "max_tokens")
        self.record(
            "max_tokens_truncation",
            "PASS" if truncated else "WARN",
            f"finish_reason={result.finish_reason}, events={len(result.events)}",
        )
        harness.close()

    def t08_bad_credentials(self) -> None:
        ws, ss = self.make_tmp()
        ws.mkdir(parents=True, exist_ok=True)
        harness = DeepSeekHarness(
            provider="deepseek-official",
            model=MODEL,
            cwd=str(ws),
            session_root=str(ss),
            base_url=BASE_URL,
            api_key="definitely-invalid-probe-key",
        )
        result, err = run_with_timeout(harness, "t08", "Say hi.")
        harness.close()
        if err is not None:
            self.record("bad_credentials", "PASS", f"clearly surfaced error: {type(err).__name__}: {str(err)[:200]}")
        else:
            self.record(
                "bad_credentials",
                "WARN",
                f"no error raised; finish_reason={result.finish_reason if isinstance(result, RunResult) else '?'}",
            )

    def run(self) -> None:
        log("=" * 78)
        log("DeepSeek Harness SDK probe")
        log("=" * 78)
        log(f"base_url : {BASE_URL}")
        log(f"api_key  : {'<set>' if API_KEY else '<EMPTY>'}")
        log(f"model    : {MODEL}")
        log(f"max_tokens: {MAX_TOKENS}")
        log(f"timeout  : {PER_TEST_TIMEOUT}s per test")
        log("-" * 78)
        if not API_KEY:
            log("凭证格式（OpenAI 兼容 chat/completions）:")
            log("  环境变量示例:")
            log("  export DSH_PROBE_BASE_URL=https://api.deepseek.com/v1   # 或任意 OpenAI 兼容代理 http://host:port/v1")
            log("  export DSH_PROBE_API_KEY=sk-xxxx                          # DeepSeek/OpenAI 风格 sk- 密钥")
            log("  export DSH_PROBE_MODEL=deepseek-chat                      # DeepSeek 官方模型 id")
            log("  运行: .venv/bin/python scripts/dsh_sdk_probe.py")
            log("-" * 78)
        self.t01_runtime_launch()
        self.t02_plain_reasoning()
        self.t03_bash_tool()
        self.t04_file_io()
        self.t05_session_persistence()
        self.t06_notification_streaming()
        self.t07_max_tokens_truncation()
        self.t08_bad_credentials()
        log("=" * 78)
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        warn = sum(1 for r in self.results if r["status"] == "WARN")
        failed = sum(1 for r in self.results if r["status"] == "FAIL")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        log(f"SUMMARY  PASS={passed} WARN={warn} FAIL={failed} SKIP={skipped}")
        log("=" * 78)
        if OUT_PATH:
            payload = {
                "config": {"base_url": BASE_URL, "model": MODEL, "api_key_set": bool(API_KEY), "max_tokens": MAX_TOKENS},
                "results": self.results,
            }
            Path(OUT_PATH).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"results written to {OUT_PATH}")
        return 1 if failed else 0


def main() -> int:
    return Probe().run()


if __name__ == "__main__":
    sys.exit(main())
