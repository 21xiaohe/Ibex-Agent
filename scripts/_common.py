"""scripts/_common.py —— 试跑脚本共用的打印工具"""
from pathlib import Path
from typing import Any

# 任务状态 → 图标，让清单更直观
_STATUS_ICON = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}


def print_reply(result: dict[str, Any]) -> None:
    print("\n=== Ibex 最终回复 ===")
    msgs = result.get("messages") or []
    print(msgs[-1].content if msgs else "（没有 messages）")


def print_files(result: dict[str, Any]) -> None:
    print("\n=== Ibex 写入的文件 ===")
    files = result.get("files") or {}
    if not files:
        print("（本次没有写文件）")
        return
    for name, data in files.items():
        print(f"\n--- {name} ---")
        # StateBackend 里文件值可能是字符串，也可能是带 content 字段的 dict
        print(data.get("content", data) if isinstance(data, dict) else data)

def print_files_fs(root: Path, subdir: str = "app") -> None:
    print("\n=== Ibex 写入的文件（磁盘）===")
    base = root / subdir
    hits = sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []
    if not hits:
        print("（磁盘上没找到文件，可能 write 失败或文件已存在被拒绝覆盖）")
        return
    for p in hits:
        print(f"\n--- {p.relative_to(root)} ---")
        print(p.read_text(encoding="utf-8"))

def print_todos(result: dict[str, Any]) -> None:
    """打印任务清单。键名经官方确认是 'todos'；为空时明确提示，避免误以为是 bug。"""
    print("\n=== Ibex 的任务清单 ===")
    todos = result.get("todos") or []
    if not todos:
        print("（本次没列计划：任务较简单，Ibex 没调用 write_todos —— 这是正常的）")
        return
    for i, todo in enumerate(todos, 1):
        # 兼容 todo 是 dict 或对象两种形态
        if isinstance(todo, dict):
            content, status = todo.get("content", str(todo)), todo.get("status", "pending")
        else:
            content, status = getattr(todo, "content", str(todo)), getattr(todo, "status", "pending")
        print(f"  {i}. {_STATUS_ICON.get(status, '•')} [{status}] {content}")


def print_all(result: dict[str, Any]) -> None:
    print_reply(result)
    print_files(result)
    print_todos(result)