"""语法分析器：将 token 序列解析为 AST。

当前支持：数字、二元运算（+ - * /）、括号。
TODO（本任务要求）：添加变量赋值（IDENT = expr）和变量引用（IDENT）的语法支持。
"""
from __future__ import annotations

from dataclasses import dataclass

from lexer import Lexer, Token


@dataclass
class NumberNode:
    value: float


@dataclass
class BinOpNode:
    op: str
    left: "ExprNode"
    right: "ExprNode"


ExprNode = NumberNode | BinOpNode


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def _current(self) -> Token:
        return self.tokens[self.pos]

    def _eat(self, type_: str) -> Token:
        tok = self._current()
        if tok.type != type_:
            raise SyntaxError(f"期望 {type_}，实际 {tok.type} ('{tok.value}') at pos {tok.pos}")
        self.pos += 1
        return tok

    def parse(self) -> list[ExprNode]:
        """解析完整输入（支持多条表达式，以换行或分号分隔）。"""
        stmts: list[ExprNode] = []
        while self._current().type != "EOF":
            stmts.append(self._expr())
        return stmts

    def _expr(self) -> ExprNode:
        """expr := term (('+' | '-') term)*"""
        node = self._term()
        while self._current().type in ("PLUS", "MINUS"):
            op = self._current().value
            self.pos += 1
            right = self._term()
            node = BinOpNode(op=op, left=node, right=right)
        return node

    def _term(self) -> ExprNode:
        """term := factor (('*' | '/') factor)*"""
        node = self._factor()
        while self._current().type in ("MUL", "DIV"):
            op = self._current().value
            self.pos += 1
            right = self._factor()
            node = BinOpNode(op=op, left=node, right=right)
        return node

    def _factor(self) -> ExprNode:
        """factor := NUMBER | '(' expr ')'"""
        tok = self._current()
        if tok.type == "NUMBER":
            self.pos += 1
            return NumberNode(value=float(tok.value))
        if tok.type == "LPAREN":
            self.pos += 1
            node = self._expr()
            self._eat("RPAREN")
            return node
        raise SyntaxError(f"意外的 token {tok.type} ('{tok.value}') at pos {tok.pos}")
