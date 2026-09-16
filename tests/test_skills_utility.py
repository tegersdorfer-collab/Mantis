import asyncio
import core.skills.utility as utility


def test_grundrechenarten_vorrang():
    """Teste Grundrechenarten mit Vorrang."""
    # (80 + 90) / 2 = 85.0
    expr = "(80 + 90) / 2"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 85.0"
    
    # 2 ** 10 = 1024
    expr = "2 ** 10"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 1024"
    
    # 7 // 2 = 3
    expr = "7 // 2"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 3"
    
    # 7 % 2 = 1
    expr = "7 % 2"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 1"


def test_vorzeichen():
    """Teste Vorzeichen."""
    # -3 + 5 = 2
    expr = "-3 + 5"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 2"


def test_math_funktionen_konstanten():
    """Teste math-Funktionen und -Konstanten."""
    # sqrt(16) = 4.0
    expr = "sqrt(16)"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 4.0"
    
    # pi ergibt eine Zahl, die mit 3.14 beginnt
    expr = "pi"
    result = asyncio.run(utility._calculate(expr))
    assert result.startswith(f"{expr} = 3.14")


def test_statistics():
    """Teste statistics."""
    # statistics.mean([1, 2, 3]) = 2
    expr = "statistics.mean([1, 2, 3])"
    result = asyncio.run(utility._calculate(expr))
    assert result == f"{expr} = 2"


def test_fehler_rueckgabe_string():
    """Teste dass Fehler als String zurueckgegeben werden, nie als Exception."""
    # 1 / 0 ergibt einen Fehler-String
    expr = "1 / 0"
    result = asyncio.run(utility._calculate(expr))
    assert result.startswith("Fehler:")


def test_sicherheit():
    """Teste Sicherheit - schadhafte Ausdrücke ergeben Fehler."""
    unsafe_exprs = [
        "__import__('os').system('echo pwned')",
        "().__class__",
        "open('/etc/passwd')",
        "statistics.__name__",
        "'a' * 3"
    ]
    for expr in unsafe_exprs:
        result = asyncio.run(utility._calculate(expr))
        assert result.startswith("Fehler:"), f"Fehler bei Ausdruck: {expr}"


def test_ungueltige_syntax():
    """Teste ungueltige Syntax."""
    # 2 + (unvollständig) ergibt einen Fehler-String
    expr = "2 +"
    result = asyncio.run(utility._calculate(expr))
    assert result.startswith("Fehler:")