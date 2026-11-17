# 订单微服务 · 团队约定（Ibex 常驻记忆）

## 项目
- 技术栈：Python 3.13 + FastAPI；包管理用 uv。
- 目录：源码在 app/，测试在 tests/，定价逻辑集中在 app/pricing.py。
  - 现状核对（Ibex 2026-02-13）：仓库里 app/pricing.py 尚不存在，实际只有 app/orders.py、
    app/discount.py；app/ 也没有 __init__.py（靠命名空间包 + 根目录在 sys.path 上导入）。
  - tests/test_orders.py 依赖一个 `client` fixture，但项目里没有 conftest.py 提供它。
  - 现状复核（Ibex，复检上一轮记录）：上两条已过时。app/__init__.py（空文件）与
    tests/conftest.py（提供 `client` fixture，只挂 app.orders 路由）均已存在，只是尚未
    提交。app/pricing.py 仍不存在；定价/金额运算实际落在 app/discount.py，入口函数
    `calc_subtotal` / `apply_coupon` / `calc_final_total`，内部 `_to_decimal` /
    `_read_field` / `_quantize` 已覆盖输入校验与 ROUND_HALF_UP 到分。
  - app/calc.py（新增）：`calc_total(items) -> str`，委托 `calc_subtotal` 后格式化成
    两位小数定点字符串。新增金额功能请先看能否复用 discount.py 的私有助手，别另写一套校验。
  - 环境提示：仓库根目录是 `/`（不是 /home/agent/...），源码在 /app、测试在 /tests。
    这条已被反复踩到：用户/上下文里给的 /home/agent/app/... 一律映射到 /app/...；`ls /home`
    直接 path_not_found。常驻记忆文件本身也挂在 /AGENTS.md，不是 Windows 路径。
  - 当前未提交清单（Ibex 2026-02-13 复核）：`?? app/calc.py`、`?? tests/test_calc.py`、
    `?? app/__init__.py`、`?? tests/conftest.py`、`?? loadtest/locustfile.py`；同时有他人
    未提交改动（`M sandbox/docker_manager.py`、`M sandbox/docker_sandbox.py`、
    `M tests/test_orders.py`、`M README.md`、`M eval/exec_scorer.py`、`M AGENTS.md`）。
    **别用 git_commit 直接提交**——它内部是 `git add -A`，会把上述无关改动一起扫进去；
    要提交先确认范围，或只提交明确的文件集合。
  - app/calc.py 已有 `tests/test_calc.py`（135 行）配套，含 HALF_UP vs HALF_EVEN 区分用例、
    指数展开、大额防科学计数法、9 种非法行。已知缺口：`Decimal.quantize` 在结果超过 28 位
    有效数字（如 1e30）时抛 `InvalidOperation` 而非 `ValueError`，与 docstring 承诺不符。
    **补充（2026-02-13）**：该异常实际先由 `discount._quantize`（`calc_subtotal` 末尾）抛出，
    根本到不了 `calc._format_money`；所以只给 calc.py 加兜底覆盖不了 `calc_total(1e30)`，
    要修得动 discount.py，或者在 calc.py 的调用处把 InvalidOperation 转成 ValueError。
  - 2026-02-13 再次收到「在 app/calc.py 实现 calc_total」的需求：文件已存在且完全满足规格
    （Decimal 运算、`Sequence[OrderItem] | None`、两位小数定点 str、空输入 "0.00"、
    非法行显式 ValueError），因此**本轮没改代码**，只做了静态核对。以后遇到重复需求先核对
    文件现状再动手，别重写一遍。
  - 本环境没有 shell/exec 工具，**无法执行 pytest**；只能静态核对，跑测试要明确告知用户
    由用户代为执行（`uv run pytest ...`），不要假装跑过。
  - 用户常把路径写成 `/home/agent/app/...`，实际就是 `/app/...`（见上面「环境提示」）。
  - app/pricing_example.py 是一段**草稿 demo 脚本**（`print` + 模块级 `getcontext().prec = 28`），
    不是生产代码，也不被任何模块导入；不要把它当成定价实现的参考或复用它。若哪天被 import，
    模块级修改全局 decimal 上下文会影响 discount.py 的 `quantize`，需保持它不被导入。

## 必须遵守（铁律）
- 金额一律用 Decimal 计算，**禁止用 float 做货币运算**。
- 所有公开函数带类型注解和 docstring。
- 任何代码改动都必须附带对应的 pytest 测试。

## 风格
- 内部命名用 snake_case；对外 API 字段用 camelCase（由 Pydantic alias 处理）。
- 提交信息用祈使句，一行概述 + 可选正文。