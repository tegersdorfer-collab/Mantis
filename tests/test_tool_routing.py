"""Unit-Tests für das Tool-Routing (core/tools.py: is_action, select_tools, _semantic_rank)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core import tools as T


async def _noop(**kwargs):
    return "ok"


@pytest.fixture
def registry():
    """Isolierte Registry mit kontrollierten Fake-Tools; Original wird restauriert."""
    saved = dict(T.REGISTRY)
    T.REGISTRY.clear()
    fakes = [
        ("create_task",         "Erstellt eine neue Aufgabe",            "productivity"),
        ("create_reminder",     "Erstellt eine Erinnerung",              "productivity"),
        ("delete_task",         "Löscht eine Aufgabe",                   "productivity"),
        ("log_meal",            "Protokolliert eine Mahlzeit",           "nutrition"),
        ("web_search",          "Sucht im Web nach Informationen",       "knowledge"),
        ("get_weather",         "Zeigt das aktuelle Wetter",             "knowledge"),
        ("api_costs",           "Zeigt Token-Verbrauch und API-Kosten",  "system"),
        ("create_skill",        "Erstellt ein neues Tool",               "system"),
        ("list_dynamic_skills", "Listet dynamische Skills",              "system"),
        ("delete_skill",        "Löscht ein dynamisches Skill",          "system"),
        ("calculate",           "Rechnet einen Ausdruck aus",            "general"),
        ("read_any_file",       "Liest den Inhalt einer beliebigen Datei", "filesystem"),
        ("list_directory",      "Listet Dateien eines Verzeichnisses auf", "filesystem"),
        ("open_app",            "Startet eine Mac-Anwendung",             "filesystem"),
        ("see_screen",          "Macht einen Screenshot und beschreibt ihn", "vision"),
        ("robot_control",       "Steuert den X5-Roboter: fahren, drehen, greifen, Sound", "robot"),
        ("robot_sensors",       "Liest die Roboter-Sensoren (IR-Abstand, Druck)", "robot"),
        ("robot_autonomy",      "Autonomer Fahrmodus des Roboters", "robot"),
    ]
    for name, desc, cat in fakes:
        T.REGISTRY[name] = T.Tool(name=name, description=desc, parameters={},
                                  handler=_noop, category=cat)
    yield T.REGISTRY
    T.REGISTRY.clear()
    T.REGISTRY.update(saved)


class TestIsAction:
    def test_erstellen_ist_aktion(self):
        assert T.is_action("Erstelle eine Aufgabe für morgen")

    def test_frage_ist_keine_aktion(self):
        assert not T.is_action("Wie war mein Schlaf letzte Nacht?")

    def test_smalltalk_ist_keine_aktion(self):
        assert not T.is_action("Danke dir!")


class TestSelectTools:
    def test_aufgaben_anfrage_liefert_productivity(self, registry):
        names = T.select_tools("Erstelle eine Aufgabe: morgen Sport machen")
        assert "create_task" in names

    def test_smalltalk_fast_path(self, registry):
        names = T.select_tools("Na, alles gut bei dir?")
        # Fast Path: nur Skill-Verwaltung + calculate
        assert set(names) <= {"create_skill", "list_dynamic_skills", "delete_skill", "calculate"}

    def test_create_skill_immer_dabei_wenn_tools_im_spiel(self, registry):
        names = T.select_tools("Erstelle eine Aufgabe für morgen")
        assert "create_skill" in names

    def test_wetterfrage_liefert_knowledge_tools(self, registry):
        names = T.select_tools("Wie ist das Wetter heute?")
        assert "get_weather" in names

    def test_wettervorhersage_fuer_morgen_liefert_wetter_tool(self, registry):
        names = T.select_tools("Wie wird das Wetter morgen?")
        assert "get_weather" in names

    def test_api_kosten_frage_liefert_system_tools(self, registry):
        names = T.select_tools("Was haben deine API-Kosten diesen Monat verursacht?")
        assert "api_costs" in names

    def test_datei_anfrage_liefert_filesystem_tools(self, registry):
        names = T.select_tools("Liste bitte die Dateien im Ordner ~/Desktop auf")
        assert "list_directory" in names

    def test_roboter_fahrbefehl_liefert_robot_tools(self, registry):
        names = T.select_tools("fahr den roboter 2 sekunden vor")
        assert "robot_control" in names

    def test_autonom_liefert_robot_autonomy(self, registry):
        names = T.select_tools("starte den autonomen modus")
        assert "robot_autonomy" in names

    def test_greifer_liefert_robot_tools(self, registry):
        names = T.select_tools("mach den greifer zu")
        assert "robot_control" in names

    def test_roboter_sensorfrage_liefert_robot_tools(self, registry):
        names = T.select_tools("was sehen die roboter-sensoren gerade?")
        assert "robot_sensors" in names

    def test_oeffne_anfrage_liefert_filesystem_tools(self, registry):
        names = T.select_tools("Öffne bitte die Datei ~/Dokumente/rechnung.pdf")
        assert "read_any_file" in names or "list_directory" in names

    def test_app_starten_liefert_filesystem_tools(self, registry):
        names = T.select_tools("Kannst du mir Notizen öffnen")
        assert "open_app" in names

    def test_bildschirm_anfrage_liefert_vision_tools(self, registry):
        names = T.select_tools("Was siehst du gerade auf meinem Bildschirm?")
        assert "see_screen" in names

    def test_cap_liegt_bei_14_plus_skill_garantie(self, registry):
        # 20 zusätzliche productivity-Tools → Auswahl bleibt gedeckelt
        for i in range(20):
            T.REGISTRY[f"extra_{i}"] = T.Tool(
                name=f"extra_{i}", description=f"Extra Aufgaben-Tool {i}",
                parameters={}, handler=_noop, category="productivity")
        names = T.select_tools("Erstelle eine Aufgabe für morgen")
        # 14er-Cap + bis zu 3 garantierte Skill-Tools
        assert len(names) <= 17


class TestSemanticRank:
    def test_findet_tool_ueber_beschreibung(self, registry):
        T.REGISTRY["crypto_price"] = T.Tool(
            name="crypto_price",
            description="Zeigt den aktuellen Bitcoin Kurs und Krypto Preise",
            parameters={}, handler=_noop, category="knowledge")
        ranked = T._semantic_rank("was macht der bitcoin kurs gerade")
        assert "crypto_price" in ranked

    def test_leere_query_liefert_nichts(self, registry):
        assert T._semantic_rank("und der die das") == []
