# Implementierungsplan: Unit-Tests für das calculate-Tool

## Schritt 1: Grundrechenarten mit Vorrang
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
import asyncio
import core.skills.utility as utility

def test_calculate_grundrechenarten_vorrang():
    # Test für (80 + 90) / 2
    expr = "(80 + 90) / 2"
    result = asyncio.run(utility._calculate(expr))
    assert result == "(80 + 90) / 2 = 85.0"

    # Test für 2 ** 10
    expr = "2 ** 10"
    result = asyncio.run(utility._calculate(expr))
    assert result == "2 ** 10 = 1024"

    # Test für 7 // 2
    expr = "7 // 2"
    result = asyncio.run(utility._calculate(expr))
    assert result == "7 // 2 = 3"

    # Test für 7 % 2
    expr = "7 % 2"
    result = asyncio.run(utility._calculate(expr))
    assert result == "7 % 2 = 1"
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich.

## Schritt 2: Vorzeichen
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
def test_calculate_vorzeichen():
    expr = "-3 + 5"
    result = asyncio.run(utility._calculate(expr))
    assert result == "-3 + 5 = 2"
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich (zusammen mit den Tests aus Schritt 1).

## Schritt 3: math-Funktionen und -Konstanten
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
def test_calculate_math_funktionen_konstanten():
    # Test für sqrt(16)
    expr = "sqrt(16)"
    result = asyncio.run(utility._calculate(expr))
    assert result == "sqrt(16) = 4.0"

    # Test für pi (Ergebnis beginnt mit 3.14)
    expr = "pi"
    result = asyncio.run(utility._calculate(expr))
    assert result.startswith("pi = 3.14")
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich (zusammen mit den vorherigen Tests).

## Schritt 4: statistics
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
def test_calculate_statistics():
    expr = "statistics.mean([1, 2, 3])"
    result = asyncio.run(utility._calculate(expr))
    assert result == "statistics.mean([1, 2, 3]) = 2"
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich (zusammen mit den vorherigen Tests).

## Schritt 5: Fehler bei Division durch Null und ähnliche
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
  def test_calculate_fehler_division_durch_null():
      expr = "1 / 0"
      result = asyncio.run(utility._calculate(expr))
      assert result.startswith("Fehler:")
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich (zusammen mit den vorherigen Tests).

## Schritt 6: Sicherheitsbeschränkungen
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
  def test_calculate_sicherheit():
      # Liste von Ausdrücken, die mit "Fehler:" antworten müssen
      dangerous_exprs = [
          "__import__('os').system('echo pwned')",
          "().__class__",
          "open('/etc/passwd')",
          "statistics.__name__",  # Attribute mit Unterstrich sind verboten
          "'a' * 3",  # Zeichenkettenausdruck
      ]
      for expr in dangerous_exprs:
          result = asyncio.run(utility._calculate(expr))
          assert result.startswith("Fehler:"), f"Fehler für Ausdruck '{expr}': {result}"
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich (zusammen mit den vorherigen Tests).

## Schritt 7: Ungültige Syntax
- Dateipfad: tests/test_skills_utility.py
- Test zuerst:
  ```python
  def test_calculate_ungueltige_syntax():
      expr = "2 +"
      result = asyncio.run(utility._calculate(expr))
      assert result.startswith("Fehler:")
  ```
- Dann führe aus: pytest tests/test_skills_utility.py
- Erwartetes Ergebnis: Der Test läuft erfolgreich (zusammen mit den vorherigen Tests).