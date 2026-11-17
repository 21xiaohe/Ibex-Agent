"""``app.discount`` 的单元测试。

覆盖：正常路径、空/None 输入、无效优惠券、VIP 等级边界、float 精度陷阱。
"""

from decimal import Decimal

import pytest

from app.discount import (
    MAX_VIP_LEVEL,
    apply_coupon,
    calc_final_total,
    calc_subtotal,
)


def assert_money(actual: Decimal, expected: str) -> None:
    """断言金额等于期望值，并校验返回的是 Decimal 且保留到「分」。"""
    assert isinstance(actual, Decimal), f"金额必须是 Decimal，实际是 {type(actual).__name__}"
    assert actual == Decimal(expected)
    assert actual.as_tuple().exponent == -2, f"金额必须保留两位小数，实际为 {actual}"


# --------------------------------------------------------------------------- #
# calc_subtotal
# --------------------------------------------------------------------------- #


def test_calc_subtotal_multiple_items() -> None:
    """多行订单（str 价格）应正确求和。"""
    items = [{"price": "10.00", "qty": 2}, {"price": "5.25", "qty": 3}]
    assert_money(calc_subtotal(items), "35.75")


def test_calc_subtotal_float_input_has_no_binary_error() -> None:
    """0.1 × 3 用 float 会得到 0.30000000000000004，Decimal 路径必须精确。"""
    assert_money(calc_subtotal([{"price": 0.1, "qty": 3}]), "0.30")


def test_calc_subtotal_accepts_decimal_price_and_quantity_alias() -> None:
    """支持 Decimal 价格，以及 ``quantity`` 字段别名。"""
    assert_money(calc_subtotal([{"price": Decimal("2.50"), "quantity": 4}]), "10.00")


def test_calc_subtotal_rounds_once_after_summing() -> None:
    """逐行 0.005 累加后统一取整，四舍五入到分。"""
    items = [{"price": "0.005", "qty": 1}, {"price": "0.005", "qty": 1}]
    assert_money(calc_subtotal(items), "0.01")


@pytest.mark.parametrize("items", [[], None])
def test_calc_subtotal_empty_returns_zero(items: list | None) -> None:
    """空列表 / None 视为空订单，小计 0.00。"""
    assert_money(calc_subtotal(items), "0.00")


def test_calc_subtotal_zero_qty_counts_as_nothing() -> None:
    """数量为 0 的行不贡献金额，但不报错。"""
    assert_money(calc_subtotal([{"price": "9.99", "qty": 0}]), "0.00")


@pytest.mark.parametrize(
    "item",
    [
        {"price": "-1.00", "qty": 1},      # 负价格
        {"price": "1.00", "qty": -1},      # 负数量
        {"qty": 1},                        # 缺价格
        {"price": "1.00"},                 # 缺数量
        {"price": "abc", "qty": 1},        # 价格无法解析
        {"price": "1.00", "qty": 1.5},     # 数量不是整数
        {"price": None, "qty": 1},         # 价格为 None
        {"price": True, "qty": 1},         # 价格是 bool
    ],
)
def test_calc_subtotal_rejects_invalid_item(item: dict) -> None:
    """非法订单行必须显式报错，不能静默算成 0。"""
    with pytest.raises(ValueError):
        calc_subtotal([item])


def test_calc_subtotal_rejects_non_mapping_item() -> None:
    """既不是 Mapping 也没有 price 属性的行，报 ValueError。"""
    with pytest.raises(ValueError):
        calc_subtotal([("price", 1)])  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# apply_coupon
# --------------------------------------------------------------------------- #

FULL_REDUCTION_100_10 = {"type": "满减", "threshold": "100.00", "amount": "10.00"}


@pytest.mark.parametrize(
    ("subtotal", "coupon", "expected"),
    [
        ("100.00", None, "100.00"),
        ("100.00", FULL_REDUCTION_100_10, "90.00"),   # 刚好达到门槛，生效
        ("99.99", FULL_REDUCTION_100_10, "99.99"),    # 未达门槛，不生效
        ("5.00", {"type": "满减", "threshold": "0", "amount": "10.00"}, "0.00"),  # 兜底到 0
        ("100.00", {"type": "打折", "rate": "0.90"}, "90.00"),   # 九折
        ("100.00", {"type": "打折", "rate": 1}, "100.00"),       # 不打折
        ("0.05", {"type": "打折", "rate": "0.5"}, "0.03"),       # 0.025 四舍五入到分
        ("0.00", {"type": "打折", "rate": "0.90"}, "0.00"),
    ],
)
def test_apply_coupon(subtotal: str, coupon: dict | None, expected: str) -> None:
    """优惠券正常路径与边界路径。"""
    assert_money(apply_coupon(Decimal(subtotal), coupon), expected)


def test_apply_coupon_accepts_float_subtotal() -> None:
    """小计入参支持 float，内部转 Decimal。"""
    assert_money(apply_coupon(36.25, None), "36.25")


@pytest.mark.parametrize(
    "coupon",
    [
        {"type": "赠品"},                                    # 不支持的券类型
        {"threshold": "100.00", "amount": "10.00"},          # 缺 type
        {"type": "满减", "threshold": "100.00"},             # 缺 amount
        {"type": "满减", "amount": "10.00"},                 # 缺 threshold
        {"type": "满减", "threshold": "-1", "amount": "5"},  # 门槛为负
        {"type": "满减", "threshold": "100", "amount": "-5"},  # 减免为负
        {"type": "打折"},                                    # 缺 rate
        {"type": "打折", "rate": "0"},                       # rate 必须 > 0
        {"type": "打折", "rate": "1.5"},                     # rate 必须 <= 1
        {"type": "打折", "rate": "abc"},                     # rate 无法解析
        "满减",                                              # 不是 Mapping
    ],
)
def test_apply_coupon_rejects_invalid_coupon(coupon: object) -> None:
    """无效优惠券必须显式报错，不做静默降级。"""
    with pytest.raises(ValueError):
        apply_coupon(Decimal("100.00"), coupon)  # type: ignore[arg-type]


def test_apply_coupon_rejects_negative_subtotal() -> None:
    """负小计是脏数据，直接报错。"""
    with pytest.raises(ValueError):
        apply_coupon(Decimal("-1.00"), None)


# --------------------------------------------------------------------------- #
# calc_final_total
# --------------------------------------------------------------------------- #


def test_calc_final_total_without_coupon_or_vip() -> None:
    """无券、非 VIP：最终金额等于小计。"""
    items = [{"price": "50.00", "qty": 2}]
    assert_money(calc_final_total(items, None, 0), "100.00")


@pytest.mark.parametrize(
    ("vip_level", "expected"),
    [
        (0, "90.00"),   # 无 VIP 折扣
        (1, "88.20"),   # -2%
        (2, "85.50"),   # -5%
        (3, "82.80"),   # -8%
        (4, "81.00"),   # -10%
        (MAX_VIP_LEVEL + 5, "81.00"),  # 超出最高等级按最高等级算
    ],
)
def test_calc_final_total_vip_ladder(vip_level: int, expected: str) -> None:
    """VIP 等级越高，应付越少；券先算，VIP 折扣叠加在券后金额上。"""
    items = [{"price": "100.00", "qty": 1}]
    assert_money(calc_final_total(items, FULL_REDUCTION_100_10, vip_level), expected)


def test_calc_final_total_vip_is_monotonic() -> None:
    """等级递增时总价单调不增。"""
    items = [{"price": "199.90", "qty": 3}]
    coupon = {"type": "打折", "rate": "0.95"}
    totals = [calc_final_total(items, coupon, level) for level in range(MAX_VIP_LEVEL + 2)]
    assert totals == sorted(totals, reverse=True)
    assert totals[0] > totals[-1]


def test_calc_final_total_empty_items() -> None:
    """空订单：无券 / 有券都返回 0.00。"""
    assert_money(calc_final_total([], None, 3), "0.00")
    assert_money(calc_final_total([], {"type": "打折", "rate": "0.90"}, 3), "0.00")


def test_calc_final_total_float_and_str_inputs_agree() -> None:
    """同一笔订单，float 与 str 入参必须得到完全一致的结果。"""
    items_float = [{"price": 10.0, "qty": 2}, {"price": 5.25, "qty": 3}]
    items_str = [{"price": "10.00", "qty": 2}, {"price": "5.25", "qty": 3}]
    coupon = {"type": "满减", "threshold": "30.00", "amount": "5.00"}
    assert calc_final_total(items_float, coupon, 2) == calc_final_total(items_str, coupon, 2)
    assert_money(calc_final_total(items_str, coupon, 2), "29.21")


@pytest.mark.parametrize("vip_level", [-1, 1.5, "3", None, True])
def test_calc_final_total_rejects_invalid_vip_level(vip_level: object) -> None:
    """VIP 等级必须是合法的非负整数。"""
    with pytest.raises(ValueError):
        calc_final_total([{"price": "10.00", "qty": 1}], None, vip_level)  # type: ignore[arg-type]


def test_calc_final_total_propagates_invalid_coupon() -> None:
    """综合计算时，无效券不能被吞掉。"""
    with pytest.raises(ValueError):
        calc_final_total([{"price": "10.00", "qty": 1}], {"type": "未知"}, 0)  # type: ignore[arg-type]
