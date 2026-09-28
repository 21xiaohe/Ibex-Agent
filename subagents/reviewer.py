# subagents/reviewer.py —— FilesystemBackend 上的物理只读 reviewer
from deepagents import FilesystemPermission

REVIEWER_PERMISSIONS = [
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),  # 禁一切写
]
# 在 create_deep_agent(..., permissions=REVIEWER_PERMISSIONS) 使用
# 注意：permissions 设了即完全替换父规则