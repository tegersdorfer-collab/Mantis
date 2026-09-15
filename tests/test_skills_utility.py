"""Unit-Tests für core/skills/utility.py: calculate-Tool."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import core.skills.utility as utility


class TestCalculate:
    def test_grundrechenarten_mit_vorrang(self):
        # (80 + 90) / 2 = 85.0
        result = asyncio.run(utility._calculate("(80 + 90) / 2"))
        assert result == "(80 + 90) / 2 = 85.0"
        # 2 ** 10 = 1024
        result = asyncio.run(utility._calculate("2 ** 10"))
        assert result == "2 ** 10 = 1024"
        # 7 // 2 = 3
        result = asyncio.run(utility._calculate("7 // 2"))
        assert result == "7 // 2 = 3"
        # 7 % 2 = 1
        result = asyncio.run(utility._calculate("7 % 2"))
        assert result == "7 % 2 = 1"

    def test_vorzeichen(self):
        # -3 + 5 = 2
        result = asyncio.run(utility._calculate("-3 + 5"))
        assert result == "-3 + 5 = 2"

    def test_math_funktionen_und_konstanten(self):
        # sqrt(16) = 4.0
        result = asyncio.run(utility._calculate("sqrt(16)"))
        assert result == "sqrt(16) = 4.0"
        # pi beginnt mit 3.14
        result = asyncio.run(utility._calculate("pi"))
        assert result.startswith("pi = 3.14")

    def test_statistics(self):
        # statistics.mean([1, 2, 3]) = 2.0
        result = asyncio.run(utility._calculate("statistics.mean([1, 2, 3])"))
        assert result == "statistics.mean([1, 2, 3]) = 2.0"

    def test_fehler_werden_als_string_zurueckgegeben(self):
        # 1 / 0 -> Fehler:
        result = asyncio.run(utility._calculate("1 / 0"))
        assert result.startswith("Fehler:")

    def test_sicherheit_blockiert_gefährliche_ausdrücke(self):
        dangerous_exprs = [
            "__import__('os').system('echo pwned')",
            "().__class__",
            "open('/etc/passwd')",
            "statistics.__name__",
            "'a' * 3",
        ]
        for expr in dangerous_exprs:
            result = asyncio.run(utility._calculate(expr))
            assert result.startswith("Fehler:"), f"Ausdruck '{expr}' sollte blockiert werden, ergab: {result}"

    def test_ungueltige_syntax(self):
        # 2 + -> Fehler:
        result = asyncio.run(utility._calculate("2 +"))
        assert result.startswith("Fehler:")