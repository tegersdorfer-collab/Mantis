# Design-Spec: Unit-Tests für das calculate-Tool

## Ziel
Die Unit-Tests für das calculate-Tool in `core/skills/utility.py` sollen die korrekte Funktion und Sicherheit des Tools gewährleisten. Sie überprüfen, dass mathematische Ausdrücke korrekt ausgewertet werden, dass mathematische Funktionen und Konstanten unterstützt werden, dass Fehler sicher als Strings zurückgegeben werden und dass gefährliche Ausdrücke blockiert werden.

## Betroffene Module
- `core/skills/utility.py`: Enthält die asynchrone Funktion `_calculate`, die getestet wird.
- `tests/test_skills_utility.py`: Die neu erstellte Testdatei mit den Unit-Tests.

## Datenfluss
In jedem Testfall wird:
1. Ein mathematischer Ausdruck als String an die Funktion `utility._calculate` übergeben.
2. Die Funktion wird mittels `asyncio.run` ausgeführt (da sie asynchron ist).
3. Der zurückgegebene String wird mit dem erwarteten Ergebnis verglichen.
4. Bei Fehlertests wird überprüft, dass der String mit "Fehler:" beginnt.

## Fehlerbehandlung
Die Unit-Tests behandeln erwartete Fehler wie folgt:
- Bei division by zero oder anderen Fehlern im Ausdruck wird erwartet, dass `_calculate` einen String zurückgibt, der mit "Fehler:" beginnt.
- Bei syntaktisch falschen Ausdrücken wird ebenfalls ein Fehlerstring erwartet.
- Bei sicherheitsrelevanten Ausdrücken (z. B. Versuchen, auf gefährliche Funktionen zuzugreifen) wird ebenfalls ein Fehlerstring erwartet.
Die Tests selbst werfen keine Ausnahmen; sie verwenden Assertions, um das erwartete Verhalten zu überprüfen.

## Was ausdrücklich NICHT gebaut wird
- Keine Änderungen am bestehenden `core/skills/utility.py`-Code.
- Keine Tests für andere Tools in `core/skills/`.
- Keine Leistungstests oder Lasttests.
- Keine Tests mit externen Ressourcen (Netzwerk, Dateisystem, Datenbank).
- Keine Tests für die Registrierung des Tools über `@T.register` (dies wird durch die Nutzung des Moduls implizit getestet).

## Getroffene Annahmen
1. Die Funktion `_calculate` ist asynchron und kann mit `asyncio.run` aufgerufen werden.
2. Das Modul `core.skills.utility` kann importiert werden, nachdem der Projektpfad zum `sys.path` hinzugefügt wurde.
3. Der AST-Walker in `_calculate` erlaubt ausschließlich die in der Implementierung definierten Operationen und Namen.
4. Die Fehlermeldungen von `_calculate` beginnen immer mit dem String "Fehler:".
5. Sicherheitseinschränkungen blockieren effektiv alle Versuche, auf gefährliche Built-ins oder Attribute mit führendem Unterstrich zuzugreifen.
6. Die Tests werden mit pytest ausgeführt und nutzen die Standard-Assertions.