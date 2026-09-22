class PaymentProcessor:
    def process_payment(self, payment_type, amount, user_id):
        if payment_type == "alipay":
            # 支付宝支付逻辑
            print(f"处理支付宝支付: 用户{user_id}, 金额{amount}")
            # 调用支付宝 API
            result = {"success": True, "transaction_id": "ALI" + str(user_id) + str(amount)}
            self._save_record("alipay", amount, user_id, result)
            return result
        elif payment_type == "wechat":
            # 微信支付逻辑
            print(f"处理微信支付: 用户{user_id}, 金额{amount}")
            # 调用微信 API
            result = {"success": True, "transaction_id": "WX" + str(user_id) + str(amount)}
            self._save_record("wechat", amount, user_id, result)
            return result
        elif payment_type == "stripe":
            # Stripe 支付逻辑
            print(f"处理 Stripe 支付: 用户{user_id}, 金额{amount}")
            # 调用 Stripe API
            result = {"success": True, "transaction_id": "STRIPE" + str(user_id) + str(amount)}
            self._save_record("stripe", amount, user_id, result)
            return result
        else:
            raise ValueError(f"不支持的支付方式: {payment_type}")

    def _save_record(self, payment_type, amount, user_id, result):
        # 保存支付记录到数据库
        import sqlite3
        conn = sqlite3.connect('payments.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO payments (user_id, amount, type, transaction_id, status) VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, payment_type, result.get("transaction_id"), "success" if result.get("success") else "failed")
        )
        conn.commit()
        conn.close()

    def refund(self, payment_type, transaction_id, amount):
        if payment_type == "alipay":
            print(f"支付宝退款: {transaction_id}, 金额{amount}")
            return {"success": True}
        elif payment_type == "wechat":
            print(f"微信退款: {transaction_id}, 金额{amount}")
            return {"success": True}
        elif payment_type == "stripe":
            print(f"Stripe 退款: {transaction_id}, 金额{amount}")
            return {"success": True}
        else:
            raise ValueError(f"不支持的支付方式: {payment_type}")

    def query_status(self, payment_type, transaction_id):
        if payment_type == "alipay":
            return {"status": "success", "amount": 100}
        elif payment_type == "wechat":
            return {"status": "success", "amount": 100}
        elif payment_type == "stripe":
            return {"status": "success", "amount": 100}
        else:
            raise ValueError(f"不支持的支付方式: {payment_type}")
