"""Unit-Tests für core/skills/utility.py: calculate-Tool."""
import asyncio
import core.skills.utility as utility


class TestCalculate:
    def test_grundrechenarten_vorrang(self):
        """(80 + 90) / 2 -> 85.0"""
        expr = "(80 + 90) / 2"
        result = asyncio.run(utility._calculate(expr))
        assert result == f"{expr} = 85.0"

    def test_potenz(self):
        """2 ** 10 -> 1024"""
        expr = "2 ** 10"
        result = asyncio.run(utility._calculate(expr))
        assert result == f"{expr} = 1024"

    def test_integer_division_modulo(self):
        """7 // 2 -> 3, 7 % 2 -> 1"""
        expr1 = "7 // 2"
        result1 = asyncio.run(utility._calculate(expr1))
        assert result1 == f"{expr1} = 3"
        expr2 = "7 % 2"
        result2 = asyncio.run(utility._calculate(expr2))
        assert result2 == f"{expr2} = 1"

    def test_vorzeichen(self):
        """-3 + 5 -> 2.0"""
        expr = "-3 + 5"
        result = asyncio.run(utility._calculate(expr))
        assert result == f"{expr} = 2.0"

    def test_math_sqrt(self):
        """sqrt(16) -> 4.0"""
        expr = "sqrt(16)"
        result = asyncio.run(utility._calculate(expr))
        assert result == f"{expr} = 4.0"

    def test_math_pi(self):
        """pi -> Ergebnis beginnt mit 3.14"""
        expr = "pi"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith(f"{expr} = 3.14")

    def test_statistics_mean(self):
        """statistics.mean([1, 2, 3]) -> 2.0"""
        expr = "statistics.mean([1, 2, 3])"
        result = asyncio.run(utility._calculate(expr))
        assert result == f"{expr} = 2.0"

    def test_division_durch_null(self):
        """1 / 0 -> Fehler: ..."""
        expr = "1 / 0"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")

    def test_sicherheit_import_os(self):
        """__import__('os').system('echo pwned') -> Fehler: ..."""
        expr = "__import__('os').system('echo pwned')"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")

    def test_sicherheit_leeres_tuple_class(self):
        """().__class__ -> Fehler: ..."""
        expr = "().__class__"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")

    def test_sicherheit_open_etc_passwd(self):
        """open('/etc/passwd') -> Fehler: ..."""
        expr = "open('/etc/passwd')"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")

    def test_sicherheit_statistics_name(self):
        """statistics.__name__ -> Fehler: ..."""
        expr = "statistics.__name__"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")

    def test_sicherheit_string_multiplication(self):
        """'a' * 3 -> Fehler: ..."""
        expr = "'a' * 3"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")

    def test_ungueltige_syntax(self):
        """2 + -> Fehler: ..."""
        expr = "2 +"
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:")