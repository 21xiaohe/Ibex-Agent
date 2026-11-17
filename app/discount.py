"""折扣计算模块：小计 → 优惠券 → VIP 额外折扣。

团队铁律（见 AGENTS.md）：金额一律用 ``Decimal`` 计算，禁止用 ``float`` 做货币运算。
本模块对外接受 ``int`` / ``float`` / ``str`` / ``Decimal`` 形式的金额输入，但会在入口处
统一转换成 ``Decimal``（``float`` 走 ``Decimal(str(value))``，绕开二进制浮点误差），
内部全程 ``Decimal`` 运算，并逐级按「分」做 ``ROUND_HALF_UP`` 四舍五入。

计算顺序固定为：小计 → 优惠券 → VIP 折扣。先券后 VIP，避免 VIP 折扣把金额压到
满减门槛以下而导致同一张券时灵时不灵；每一级都落到「分」，保证金额可复现。

Example:
    >>> calc_subtotal([{"price": "10.00", "qty": 2}, {"price": "5.25", "qty": 3}])
    Decimal('35.75')
    >>> calc_final_total(
    ...     [{"price": "100.00", "qty": 1}],
    ...     {"type": "满减", "threshold": "100.00", "amount": "10.00"},
    ...     vip_level=3,
    ... )
    Decimal('82.80')
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "COUPON_TYPE_DISCOUNT",
    "COUPON_TYPE_FULL_REDUCTION",
    "MAX_VIP_LEVEL",
    "Coupon",
    "DiscountCoupon",
    "FullReductionCoupon",
    "OrderItem",
    "apply_coupon",
    "calc_final_total",
    "calc_subtotal",
]

Money = Decimal | int | float | str
"""可接受的金额输入类型；``float`` 会在入口处经 ``Decimal(str(v))`` 安全转换。"""

CENT: Decimal = Decimal("0.01")
"""金额最小单位：分。"""

ZERO: Decimal = Decimal("0")
ONE: Decimal = Decimal("1")

COUPON_TYPE_FULL_REDUCTION: str = "满减"
"""满减券类型标识。"""

COUPON_TYPE_DISCOUNT: str = "打折"
"""打折券类型标识。"""

#: VIP 等级 -> 额外折扣率（在优惠券之后再叠加，等级越高折扣越多）。
_VIP_DISCOUNT_RATES: dict[int, Decimal] = {
    0: Decimal("0"),
    1: Decimal("0.02"),
    2: Decimal("0.05"),
    3: Decimal("0.08"),
    4: Decimal("0.10"),
}

MAX_VIP_LEVEL: int = max(_VIP_DISCOUNT_RATES)
"""VIP 折扣的最高等级；传入更高等级时按该等级计算。"""


class OrderItem(TypedDict):
    """订单行。

    Attributes:
        price: 单价，必须为非负数。
        qty: 数量，必须为非负整数；与 ``quantity`` 二者择一，``qty`` 优先。
        quantity: ``qty`` 的别名，便于复用 ``app.orders.OrderItem`` 的字段命名。
    """

    price: Money
    qty: NotRequired[int]
    quantity: NotRequired[int]


class FullReductionCoupon(TypedDict):
    """满减券：小计达到 ``threshold`` 时立减 ``amount``。

    Attributes:
        type: 固定为 ``"满减"``。
        threshold: 使用门槛，非负数；小计 **达到**（>=）门槛才生效。
        amount: 减免金额，非负数；减免后金额不会为负。
    """

    type: Literal["满减"]
    threshold: Money
    amount: Money


class DiscountCoupon(TypedDict):
    """打折券：小计乘以 ``rate``。

    Attributes:
        type: 固定为 ``"打折"``。
        rate: 折扣系数，取值 ``(0, 1]``；``0.9`` 表示九折（付 90%），``1`` 表示不打折。
    """

    type: Literal["打折"]
    rate: Money


Coupon = FullReductionCoupon | DiscountCoupon
"""优惠券：满减券或打折券。"""


def _to_decimal(value: Any, *, field: str) -> Decimal:
    """把金额输入安全转换为 ``Decimal``。

    ``float`` 先转成 ``str`` 再进 ``Decimal``，避免 ``Decimal(0.1)`` 这类二进制误差。

    Args:
        value: 待转换的金额输入，支持 ``Decimal`` / ``int`` / ``float`` / ``str``。
        field: 字段名，仅用于组装报错信息。

    Returns:
        等值的 ``Decimal``。

    Raises:
        ValueError: 值为空、类型不支持、字符串无法解析、或为 NaN / Infinity。
    """
    if value is None:
        raise ValueError(f"{field} 不能为空")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        # bool 是 int 的子类，但金额里出现 True/False 基本都是数据错误。
        raise ValueError(f"{field} 不能是布尔值，实际为 {value!r}")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"{field} 无法解析为金额：{value!r}") from exc
    else:
        raise ValueError(f"{field} 类型不支持：{type(value).__name__}")
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数值，实际为 {value!r}")
    return result


def _to_qty(value: Any, *, field: str) -> int:
    """校验数量字段。

    Args:
        value: 待校验的数量。
        field: 字段名，仅用于组装报错信息。

    Returns:
        合法的非负整数数量。

    Raises:
        ValueError: 值不是整数、是布尔值、或为负数。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数，实际为 {value!r}")
    if value < 0:
        raise ValueError(f"{field} 不能为负数，实际为 {value!r}")
    return value


def _read_field(item: Any, names: Sequence[str], *, field: str) -> Any:
    """从订单行中读取字段，兼容 ``Mapping``（dict）和带同名属性的对象（如 Pydantic 模型）。

    Args:
        item: 订单行。
        names: 候选字段名，按顺序取第一个命中的。
        field: 报错时使用的字段描述。

    Returns:
        字段值。

    Raises:
        ValueError: 所有候选字段都不存在。
    """
    for name in names:
        if isinstance(item, Mapping):
            if name in item:
                return item[name]
        elif hasattr(item, name):
            return getattr(item, name)
    raise ValueError(f"{field} 缺少必需字段 {names[0]!r}")


def _quantize(value: Decimal) -> Decimal:
    """把金额按「分」四舍五入（``ROUND_HALF_UP``）。

    Args:
        value: 待取整的金额。

    Returns:
        保留两位小数、四舍五入到分的金额。
    """
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _vip_discount_rate(vip_level: int) -> Decimal:
    """查询 VIP 等级的额外折扣率。

    Args:
        vip_level: VIP 等级，非负整数；超过 ``MAX_VIP_LEVEL`` 时按最高等级计算。

    Returns:
        折扣率，``Decimal("0.08")`` 表示额外 8% off。

    Raises:
        ValueError: 等级不是非负整数。
    """
    if isinstance(vip_level, bool) or not isinstance(vip_level, int):
        raise ValueError(f"vip_level 必须是整数，实际为 {vip_level!r}")
    if vip_level < 0:
        raise ValueError(f"vip_level 不能为负数，实际为 {vip_level!r}")
    return _VIP_DISCOUNT_RATES[min(vip_level, MAX_VIP_LEVEL)]


def calc_subtotal(items: Sequence[OrderItem] | None) -> Decimal:
    """计算订单小计（单价 × 数量 求和）。

    先累加后用 ``ROUND_HALF_UP`` 四舍五入到分（不是逐行取整），避免舍入误差累积。

    Args:
        items: 订单行序列，形如 ``[{"price": 10.0, "qty": 2}, ...]``；数量字段也接受
            ``quantity``。传入 ``None`` 或空序列时小计为 ``0.00``。

    Returns:
        小计金额，``Decimal``，保留两位小数。

    Raises:
        ValueError: 某行缺 ``price``、价格/数量为负、价格无法解析、数量不是整数等情况。

    Example:
        >>> calc_subtotal([{"price": "10.00", "qty": 2}])
        Decimal('20.00')
    """
    if not items:
        return _quantize(ZERO)

    subtotal = ZERO
    for index, item in enumerate(items):
        price = _to_decimal(
            _read_field(item, ("price",), field=f"items[{index}]"),
            field=f"items[{index}].price",
        )
        if price < 0:
            raise ValueError(f"items[{index}].price 不能为负数，实际为 {price}")
        qty = _to_qty(
            _read_field(item, ("qty", "quantity"), field=f"items[{index}]"),
            field=f"items[{index}].qty",
        )
        subtotal += price * qty
    return _quantize(subtotal)


def apply_coupon(subtotal: Money, coupon: Coupon | None) -> Decimal:
    """对小计应用优惠券。

    Args:
        subtotal: 小计金额，支持 ``Decimal`` / ``int`` / ``float`` / ``str``。
        coupon: 优惠券，``None`` 表示不使用优惠券。满减券形如
            ``{"type": "满减", "threshold": "100.00", "amount": "10.00"}``；打折券形如
            ``{"type": "打折", "rate": "0.90"}``。

    Returns:
        优惠后金额，``Decimal``，保留两位小数，不会小于 ``0.00``。

    Raises:
        ValueError: 小计为负/无法解析；优惠券不是 ``Mapping``；券类型不支持；缺少
            ``threshold`` / ``amount`` / ``rate``；金额或折扣率取值越界。

    Example:
        >>> apply_coupon(Decimal("100.00"), {"type": "打折", "rate": "0.90"})
        Decimal('90.00')
    """
    base = _quantize(_to_decimal(subtotal, field="subtotal"))
    if base < 0:
        raise ValueError(f"subtotal 不能为负数，实际为 {base}")
    if coupon is None:
        return base
    if not isinstance(coupon, Mapping):
        raise ValueError(f"coupon 必须是 dict 或 None，实际为 {type(coupon).__name__}")

    coupon_type = coupon.get("type")
    if coupon_type == COUPON_TYPE_FULL_REDUCTION:
        threshold = _to_decimal(coupon.get("threshold"), field="coupon.threshold")
        amount = _to_decimal(coupon.get("amount"), field="coupon.amount")
        if threshold < 0:
            raise ValueError(f"coupon.threshold 不能为负数，实际为 {threshold}")
        if amount < 0:
            raise ValueError(f"coupon.amount 不能为负数，实际为 {amount}")
        if base < threshold:
            # 未达门槛：券不生效，金额原样返回。
            return base
        return _quantize(max(base - amount, ZERO))

    if coupon_type == COUPON_TYPE_DISCOUNT:
        rate = _to_decimal(coupon.get("rate"), field="coupon.rate")
        if not (ZERO < rate <= ONE):
            raise ValueError(f"coupon.rate 必须落在 (0, 1]，实际为 {rate}")
        return _quantize(base * rate)

    raise ValueError(
        f"不支持的优惠券类型：{coupon_type!r}，仅支持 "
        f"{COUPON_TYPE_FULL_REDUCTION!r} 和 {COUPON_TYPE_DISCOUNT!r}"
    )


def calc_final_total(
    items: Sequence[OrderItem] | None,
    coupon: Coupon | None = None,
    vip_level: int = 0,
) -> Decimal:
    """综合计算最终应付金额：小计 → 优惠券 → VIP 额外折扣。

    每一步都按「分」四舍五入后再进入下一步，保证结果可复现、可对账。

    Args:
        items: 订单行序列，语义同 :func:`calc_subtotal`。
        coupon: 优惠券，语义同 :func:`apply_coupon`；``None`` 表示不使用。
        vip_level: VIP 等级，非负整数，默认 ``0``；等级越高额外折扣越多，超过
            :data:`MAX_VIP_LEVEL` 按最高等级计算。

    Returns:
        最终应付金额，``Decimal``，保留两位小数，不会小于 ``0.00``。

    Raises:
        ValueError: 任一环节的数据非法（订单行、优惠券、VIP 等级），异常会原样抛出，
            不做静默降级，避免把脏数据算进账。

    Example:
        >>> calc_final_total([{"price": "100.00", "qty": 1}], None, vip_level=2)
        Decimal('95.00')
    """
    subtotal = calc_subtotal(items)
    after_coupon = apply_coupon(subtotal, coupon)
    rate = _vip_discount_rate(vip_level)
    if rate == ZERO:
        return after_coupon
    return _quantize(after_coupon * (ONE - rate))
