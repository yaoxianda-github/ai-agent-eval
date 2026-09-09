"""求值器：遍历 AST 计算表达式值。

当前支持：数字、二元运算。
TODO（本任务要求）：添加变量环境（赋值写入 env、引用从 env 读取）。
"""
from __future__ import annotations

from parser import BinOpNode, ExprNode, NumberNode


class Evaluator:
    def __init__(self):
        # TODO: 添加变量环境字典，如 self.env: dict[str, float] = {}
        pass

    def eval_program(self, stmts: list[ExprNode]) -> float:
        """执行多条语句，返回最后一条表达式的值。"""
        result = 0.0
        for stmt in stmts:
            result = self._eval_expr(stmt)
        return result

    def _eval_expr(self, node: ExprNode) -> float:
        if isinstance(node, NumberNode):
            return node.value
        if isinstance(node, BinOpNode):
            left = self._eval_expr(node.left)
            right = self._eval_expr(node.right)
            if node.op == "+":
                return left + right
            if node.op == "-":
                return left - right
            if node.op == "*":
                return left * right
            if node.op == "/":
                if right == 0:
                    raise ZeroDivisionError("除数不能为零")
                return left / right
        raise ValueError(f"未知节点类型: {type(node).__name__}")
