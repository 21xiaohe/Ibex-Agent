from decimal import Decimal, getcontext

# 设置 Decimal 的精度
getcontext().prec = 28  # 根据需求设置合适的精度

# 使用 Decimal 来表示货币值
price = Decimal('19.99')
tax_rate = Decimal('0.05')  # 5% 税率
quantity = 3

# 计算总价和税额
total_price = price * quantity
total_tax = total_price * tax_rate
grand_total = total_price + total_tax

print(f"Total Price: {total_price}")
print(f"Total Tax: {total_tax}")
print(f"Grand Total: {grand_total}")