# -*- coding: utf-8 -*-
from money import parse_amount

def test_basic():
    assert parse_amount("1,234.56") == 1234.56

def test_dollar():
    assert parse_amount("$100") == 100.0

def test_negative():
    assert parse_amount("-50") == -50.0

def test_cents():
    assert parse_amount("0.99") == 0.99

def test_thousands_no_cents():
    assert parse_amount("1,000") == 1000.0

def test_negative_dollar():
    assert parse_amount("-$50.5") == -50.5
