"""eval/exec_scorer.py —— 客观评分：沙箱 pytest 通过率 + 规范静态检查

复用沙箱池（create_one_sandbox / seed_project / destroy_sandbox）。
客观评分结果是 0/1（测试过则 1）——能运行验证的任务结果是确定的，不需要模糊打分。

关键修复（2026-11-17）：**必须给 agent 绑沙箱 backend**。
此前直接用 build_agent()（默认本地 FilesystemBackend），agent 把代码写到宿主机
而不是沙箱——沙箱里空空如也，隐藏测试收集失败，exec 恒为 0.0（baseline 就是
这么来的）；而宿主机文件被下一次 seed 带进沙箱后又会出现"假阳性 1.0"。
修复后每个用例：建沙箱 → seed → 建绑沙箱 backend 的 agent → 实现 → 隐藏测试
→ pytest 评分 → 销毁，全程闭环在沙箱内。
"""
import ast

from langgraph.checkpoint.memory import MemorySaver

from agent.main import IBEX_SYSTEM_PROMPT, SANDBOX_PATH_RULES, build_model
from infra.settings import get_settings
from sandbox.docker_manager import create_one_sandbox, seed_project, destroy_sandbox


async def score_by_execution(case: dict) -> dict:
    """让 agent 在沙箱里实现，写入隐藏测试，跑 pytest，按通过情况打分。

    注意签名与 run_eval 的开放题评分器不同：这里不接收外部 agent——
    agent 必须和本函数自建的沙箱绑定，跨用例复用外部 agent 反而会写错地方。
    """
    from deepagents import create_deep_agent

    sandbox = await create_one_sandbox()
    try:
        await seed_project(sandbox)
        workdir = get_settings().sandbox_workdir

        # 绑沙箱 backend：文件工具（write_file/edit_file）与 execute 全部落进沙箱，
        # 路径规则进 system prompt，防止 agent 写相对路径/根路径
        agent = create_deep_agent(
            model=build_model(),
            system_prompt=IBEX_SYSTEM_PROMPT + SANDBOX_PATH_RULES.format(workdir=workdir),
            backend=sandbox,
            skills=[f"{workdir}/skills"],
            memory=[f"{workdir}/AGENTS.md"],
            checkpointer=MemorySaver(),
        )
        # ① agent 在沙箱里实现
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": case["issue"]}]},
            config={"configurable": {"thread_id": f"eval-{case['id']}"}},
        )
        # ② 写入隐藏测试（覆盖 agent 可能写的同名测试）
        #    上传失败必须当场暴露——upload_files 是"部分成功"协议（不抛异常，
        #    失败记在 error 字段），静默吞掉会导致误判成 agent 实现问题
        uploads = sandbox.upload_files([(case["test_file"], case["test_code"].encode("utf-8"))])
        err = uploads[0].error
        if err:
            return {
                "id": case["id"], "category": "exec",
                "score": 0.0, "static": {},
                "detail": f"隐藏测试上传失败（基础设施问题，非 agent 问题）：{err}",
            }
        # ③ 跑 pytest 看通过
        r = sandbox.execute("cd /home/agent && python -m pytest -q 2>&1 | tail -5")
        passed = ("passed" in r.output
                  and "failed" not in r.output and "error" not in r.output.lower())
        # ④ 静态检查：取回代码验规范（货币没用 float、函数有类型注解）
        target = case.get("target_file", "")
        code = b""
        if target:
            dl = sandbox.download_files([f"/home/agent/{target}"])
            code = dl[0].content if dl and dl[0].content else b""
        static = _static_checks(code)
        return {
            "id": case["id"], "category": "exec",
            "score": 1.0 if passed else 0.0,      # 客观：过则 1，不过则 0
            "static": static,                     # 规范遵守的附加维度
            # 失败时多留上下文（只留 200 字经常看不出收集错误根因）
            "detail": r.output[-800:],
        }
    finally:
        await destroy_sandbox(sandbox)


def _static_checks(code: bytes) -> dict:
    """AST 静态检查：量化 AGENTS.md 铁律有没有被遵守。

    简化版：出现 float( 调用就算违规（真实项目要更精细判断是否用于金额，
    比如只在金额相关变量/函数里查；这里粗判，作为示意）。
    """
    out = {"no_float_money": True, "has_type_hints": True}
    try:
        tree = ast.parse(code.decode("utf-8", errors="replace"))
    except SyntaxError:
        return {"no_float_money": False, "has_type_hints": False, "parse_error": True}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
            out["no_float_money"] = False
        if isinstance(node, ast.FunctionDef):
            if node.returns is None or any(a.annotation is None for a in node.args.args):
                out["has_type_hints"] = False
    return out