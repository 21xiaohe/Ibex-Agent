"""scripts/demo_16_bg_task.py —— 方案一：BackgroundTasks 任务提交 + 轮询

先启动服务：uv run uvicorn api.app:app --port 8000
运行：uv run python -m scripts.demo_16_bg_task
"""
import asyncio
import httpx

BASE = "http://localhost:8000"


async def main():
    async with httpx.AsyncClient(timeout=300) as client:
        # ① 提交 —— 立即拿 task_id（服务没等它跑完）
        r = await client.post(f"{BASE}/tasks", json={
            "text": "用一句话解释什么是异步任务。", "user_id": "alice",
        })
        task_id = r.json()["task_id"]
        print("① 已提交，task_id =", task_id, "（秒级返回）")

        # ② 轮询查表 —— 直到 done
        for i in range(60):
            await asyncio.sleep(2)
            data = (await client.get(f"{BASE}/tasks/{task_id}")).json()
            print(f"   第{i+1}次：status={data['status']}")
            if data["status"] in ("done", "error"):
                print("\n② 最终：", data.get("result") or data.get("error"))
                break


if __name__ == "__main__":
    asyncio.run(main())