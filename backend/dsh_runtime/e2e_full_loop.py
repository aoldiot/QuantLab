"""E2E: DSH real-key full loop via the HTTP bridge.

Creates a research project, kicks off a DSH turn that (1) reads the existing
valid strategy as reference, (2) proposes a write_strategy_code change and stops
for approval, (3) after approval runs it back and proposes execute_backtest_tool,
(4) after approval runs the backtest and summarizes.
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api"


def req(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers, method=method))
    return json.loads(r.read().decode())


def login():
    return req("POST", "/auth/login", {"username": "admin", "password": "admin123"})["access_token"]


def main():
    token = login()
    proj = req("POST", "/research", {"title": "DSH E2E " + time.strftime("%H%M%S")}, token)
    pid = proj["id"]
    print("project:", pid, flush=True)

    prompt = (
        "请完成一个量化研究闭环。步骤：\n"
        "1) 先读取既有策略 backend/app/strategies/beat_bollinger_4h_direction_runtime.py 作为参考（用 dispatch_tool_call 的 quant_get_strategy 或 bash cat）。\n"
        "2) 基于它写一个简化变体策略（改名 test_dsh_e2e，改动 1-2 个参数默认值即可），用 write_strategy_code 提交——该工具会返回 awaiting_approval，请就此结束本轮回合并明确请求审批。\n"
        "3) 用户批准后，用 verify_strategy_file 确认，再用 execute_backtest_tool 请求回测——同样会返回 awaiting_approval，请结束本轮请求审批。\n"
        "4) 回测获批后将收到 run_id 与绩效摘要，请用中文输出简洁的最终总结。"
    )
    req("POST", f"/research/{pid}/dsh/run", {"content": prompt}, token)
    print("kicked off; polling pending approvals...", flush=True)

    deadline = time.time() + 420
    approved_ids = set()
    round_no = 0
    while time.time() < deadline:
        time.sleep(3)
        st = req("GET", f"/research/{pid}/thinking-status", token=token)
        pending = req("GET", f"/research/{pid}/dsh/pending", token=token)
        msgs = req("GET", f"/research/{pid}/messages", token=token)
        runs = req("GET", f"/research/{pid}/backtests", token=token)
        active = [r for r in runs if r.get("status") in ("QUEUED", "RUNNING", "ANALYZING")]
        print(f"[{round_no}] status={st.get('status')} pending={len(pending)} msgs={len(msgs)} runs={len(runs)} active={len(active)}", flush=True)

        if pending:
            for p in pending:
                rid = p["request_id"]
                if rid in approved_ids or p.get("status") != "pending":
                    continue
                if p["tool"] == "write_strategy_code":
                    # Approve the code proposal; ask for a small tweak
                    print(f"  -> approving code proposal {rid}", flush=True)
                    req("POST", f"/research/{pid}/dsh/approve", {"request_id": rid, "approved": True, "feedback": "改个参数默认值即可"}, token)
                    approved_ids.add(rid)
                elif p["tool"] == "execute_backtest_tool":
                    print(f"  -> approving backtest proposal {rid}", flush=True)
                    req("POST", f"/research/{pid}/dsh/approve", {"request_id": rid, "approved": True, "feedback": ""}, token)
                    approved_ids.add(rid)
        if not pending and not active:
            last = msgs[-1] if msgs else None
            if last and last["role"] == "assistant" and "最终" in (last.get("content") or ""):
                print("DONE. Final assistant message:", flush=True)
                print(last["content"][:1500], flush=True)
                return
        round_no += 1

    print("TIMEOUT reached. Latest state:", flush=True)
    print(json.dumps({"pending": pending, "msgs": msgs[-3:] if msgs else [], "runs": runs}, ensure_ascii=False, default=str)[:2500], flush=True)
    sys.exit(2)


if __name__ == "__main__":
    main()