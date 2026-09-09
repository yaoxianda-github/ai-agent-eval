"""词法分析器：将表达式字符串切分为 token 序列。

当前支持：数字、+ - * / ( )。
TODO（本任务要求）：添加标识符（IDENT）和赋值号（ASSIGN）的词法支持。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Token:
    type: str  # NUMBER | PLUS | MINUS | MUL | DIV | LPAREN | RPAREN | EOF
    value: str
    pos: int


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.pos < len(self.text):
            ch = self.text[self.pos]
            if ch.isspace():
                self.pos += 1
                continue
            if ch.isdigit() or (ch == "." and self.pos + 1 < len(self.text) and self.text[self.pos + 1].isdigit()):
                start = self.pos
                while self.pos < len(self.text) and (self.text[self.pos].isdigit() or self.text[self.pos] == "."):
                    self.pos += 1
                tokens.append(Token("NUMBER", self.text[start:self.pos], start))
            elif ch == "+":
                tokens.append(Token("PLUS", ch, self.pos)); self.pos += 1
            elif ch == "-":
                tokens.append(Token("MINUS", ch, self.pos)); self.pos += 1
            elif ch == "*":
                tokens.append(Token("MUL", ch, self.pos)); self.pos += 1
            elif ch == "/":
                tokens.append(Token("DIV", ch, self.pos)); self.pos += 1
            elif ch == "(":
                tokens.append(Token("LPAREN", ch, self.pos)); self.pos += 1
            elif ch == ")":
                tokens.append(Token("RPAREN", ch, self.pos)); self.pos += 1
            else:
                raise ValueError(f"未知字符 '{ch}' at pos {self.pos}")
        tokens.append(Token("EOF", "", self.pos))
        return tokens
