"""Handgelabelte Fälle für den Jev-vs-lokal-Vergleich.

Jeder Fall bildet eine Entscheidung nach, die Mantis heute per Prompt trifft
(siehe core/tools.py, core/voice.py, proactive.py, domains/task_executor.py,
domains/second_brain.py, memory/extractor.py, memory/conflict.py).

Felder: id, kind, state (str|dict), expected (Label), optional note.
Alle Personen, Orte, Firmen und Projekte in den Fällen sind fiktiv (Repo ist öffentlich).
Die Labels sind bewusst auch mit Grenzfällen bestückt — ein Router, der nur
die einfachen trifft, hilft Mantis nicht.
"""

INTENT_CRITERIA = {
    "fitness":      "Training, Workouts, Sport-Einheiten protokollieren",
    "nutrition":    "Essen, Mahlzeiten, Kalorien, Trinken protokollieren",
    "productivity": "Aufgaben, Termine, Erinnerungen, Notizen anlegen oder abfragen",
    "health":       "Schlaf, Puls, HRV, Erholung, Körperdaten abfragen",
    "knowledge":    "Wissensfragen, Websuche, Wetter, Nachrichten, Preise",
    "habits":       "Gewohnheiten abhaken oder Streaks abfragen",
    "goals":        "Langfristige Ziele setzen oder Fortschritt abfragen",
    "robot":        "Den Roboter fahren, drehen, stoppen",
    "flipper":      "IR-Geräte steuern: Schreibtischlampe, Ventilator",
    "chat":         "Smalltalk, Meinung, Witz — keine Aktion und kein Datenabruf",
}

INTENT = [
    ("i01", "Ich hab heute 30 Minuten Oberkörper trainiert", "fitness"),
    ("i02", "Trag bitte ein dass ich 3 Eier zum Frühstück hatte", "nutrition"),
    ("i03", "Erinnere mich morgen um 9 an den Zahnarzt", "productivity"),
    ("i04", "Wie hab ich letzte Nacht geschlafen?", "health"),
    ("i05", "Was kostet ein Flug nach Tokio gerade so?", "knowledge"),
    ("i06", "Hab meine Meditation heute abgehakt", "habits"),
    ("i07", "Mein Ziel ist es bis Dezember 90 Kilo zu wiegen", "goals"),
    ("i08", "Fahr den Roboter zwei Sekunden vor", "robot"),
    ("i09", "Mach die Schreibtischlampe an", "flipper"),
    ("i10", "Erzähl mir einen Witz", "chat"),
    # Grenzfälle
    ("i11", "Licht aus", "flipper"),
    ("i12", "Wie war mein Puls beim Laufen gestern?", "health"),        # Sport, aber Datenabruf Körper
    ("i13", "Ich war laufen, 8 km in 45 Minuten", "fitness"),
    ("i14", "Was soll ich heute essen?", "chat"),                        # Empfehlung, kein Protokoll
    ("i15", "Hab ich diese Woche schon dreimal Sport gemacht?", "habits"),
    ("i16", "Wie viel Kalorien hat eine Banane?", "knowledge"),
    ("i17", "Ich hab eine Banane gegessen", "nutrition"),
    ("i18", "Stopp!", "robot"),
    ("i19", "Nach links drehen", "robot"),
    ("i20", "Ventilator eine Stufe höher", "flipper"),
    ("i21", "Was steht morgen an?", "productivity"),
    ("i22", "Regnet es morgen in Nürnberg?", "knowledge"),
    ("i23", "Ich will bis Sommer 10 Bücher gelesen haben", "goals"),
    ("i24", "Danke, das war's für heute", "chat"),
    ("i25", "Wie läuft mein Streak bei Zähneputzen abends?", "habits"),
    ("i26", "Notier dir: Ideen für Mantis — Jev als Router testen", "productivity"),
]

ADDRESS = [   # Sprach-Transkripte ohne Wake-Word: an Mantis gerichtet?
    ("a01", "Mantis, wie spät ist es?", True),
    ("a02", "boah bin ich müde heute...", False),
    ("a03", "kannst du mir kurz das Wetter sagen", True),
    ("a04", "ja ne das passt schon so glaub ich", False),
    ("a05", "mach mal das Licht an", True),
    ("a06", "ich glaub ich mach mir gleich noch einen Kaffee", False),
    ("a07", "wie viel Uhr haben wir", True),
    ("a08", "hey hast du das gesehen gestern das Spiel", False),        # an eine Person
    ("a09", "erinner mich nachher an die Wäsche", True),
    ("a10", "wo hab ich jetzt schon wieder mein Handy hingelegt", False),  # Selbstgespräch
    ("a11", "spiel was von Billie Eilish", True),
    ("a12", "nee lass mal, ich mach das später selber", False),
    ("a13", "was steht heute noch an", True),
    ("a14", "der Typ gestern war echt anstrengend ey", False),
]

GATE = [   # AKTION (Tool nötig) vs GESPRAECH
    ("g01", "Leg eine Aufgabe an: Steuer machen", True),
    ("g02", "Was denkst du über künstliche Intelligenz?", False),
    ("g03", "Wie war mein Schlaf?", True),                # Datenabruf = Tool
    ("g04", "Findest du es okay, heute nichts zu tun?", False),
    ("g05", "Such mal raus wann die Bahn nach München fährt", True),
    ("g06", "Ich bin heute irgendwie schlecht drauf", False),
    ("g07", "Hak Meditation ab", True),
    ("g08", "Erklär mir kurz was ein Transformer ist", False),   # Wissen aus dem Modell, kein Tool
    ("g09", "Was hab ich gestern gegessen?", True),
    ("g10", "Danke dir!", False),
]

PROACTIVE = [   # Generierter Gedanke — JETZT an Timo schicken? (strenge Kriterien, im Zweifel NEIN)
    ("p01", "Du hast heute noch keinen Anker gesetzt und es ist 14 Uhr — willst du kurz einen setzen?", False,
     "Klingt nach Coaching/Erinnerung ohne neue Info"),
    ("p02", "Die Bahn nach München um 8:12 morgen ist laut DB gestrichen, die 8:42 fährt.", True,
     "Konkret, neu, handlungsrelevant"),
    ("p03", "Denk daran, genug Wasser zu trinken!", False, "Generisch, Moralappell"),
    ("p04", "Du hast gestern gesagt du willst Jonas wegen dem Fahrrad anrufen — das steht noch offen.", True,
     "Offene Aktion, konkret"),
    ("p05", "Ich habe bemerkt, dass du in letzter Zeit oft spät ins Bett gehst. Das könnte deine Erholung beeinträchtigen.", False,
     "Verhaltensanalyse/Coaching"),
    ("p06", "Ich schicke dir gleich eine Zusammenfassung der News.", False, "Kündigt an statt zu liefern"),
    ("p07", "Morgen 9:00 Zahnarzt — Praxis hat per Mail nach Versichertenkarte gefragt, liegt die noch im alten Portemonnaie?", True,
     "Neu aus Mail, konkret"),
    ("p08", "Schöner Tag heute! Genieß ihn.", False, "Austauschbar"),
    ("p09", "Ich habe deine Kündigung fürs Fitnessstudio abgeschickt.", False,
     "Behauptet Aktion, die so nicht passiert ist (Mantis sendet nichts ohne Freigabe)"),
    ("p10", "Der Flipper meldet seit 20 Minuten keine Verbindung mehr — die Lampe reagiert gerade nicht.", True,
     "Systemzustand, neu, relevant"),
]

INBOX_CRITERIA = {
    "context":  "Infos über Timo selbst: Schreibstil, Background, Präferenzen",
    "project":  "Aktives Projekt mit Ziel oder Deadline",
    "area":     "Laufende Verantwortlichkeit ohne festes Ende",
    "resource": "Allgemeines Wissen, Recherche, Tool-Doku",
    "daily":    "Tagesnotiz — was heute passiert ist",
    "archive":  "Erledigt, nicht mehr relevant",
}

INBOX = [
    ("n01", {"title": "Jev / TypeSafe", "content": "System-One-Modell, drei Primitive Noul/Choice/Score, $0.042/1M, OpenRouter Decisions-Endpoint alpha"}, "resource"),
    ("n02", {"title": "Wohnungssuche", "content": "Bis 30.09. drei Besichtigungen, Unterlagen-Mappe fertig, Portal-Upload hakt bei PDF > 5 MB"}, "project"),
    ("n03", {"title": "Ich schreibe lieber kurz", "content": "Mag keine Floskeln, direkt und locker auf Deutsch, ehrliches Feedback"}, "context"),
    ("n04", {"title": "Heute", "content": "Vormittag Website-Redesign fertig, nachmittags Jev-Recherche, abends Lauf 6 km"}, "daily"),
    ("n05", {"title": "Heimserver", "content": "Alter Laptop läuft als 24/7-Server für Backups und Pi-hole — regelmäßig Updates einspielen"}, "area"),
    ("n06", {"title": "Spiel-Prototyp v7", "content": "Alle Level fertig, Design-Overhaul done, Build v7 veröffentlicht. Abgeschlossen."}, "archive"),
    ("n07", {"title": "Ollama Modelfile-Gotcha", "content": "Neue Modellversionen verlieren SYSTEM-Prompt der Vorversion; vor Upgrade ollama show --modelfile diffen"}, "resource"),
    ("n08", {"title": "Mantis Accountability", "content": "Modul pflegen: Daily Anchor + Block laufen, Roadmap C–F offen, Prinzip: statische Strings, kein Coaching"}, "area"),
]

TASK_CLASSIFY = [   # Übernimmt Mantis die Aufgabe oder muss Timo das selbst (physisch)?
    ("t01", {"title": "Recherchiere Kletterhallen im Umkreis von 30 km", "notes": None}, "mantis"),
    ("t02", {"title": "Paket bei der Post abholen", "notes": "Benachrichtigungskarte liegt im Flur"}, "user"),
    ("t03", {"title": "Trainingsdaten der letzten 4 Wochen auswerten", "notes": None}, "mantis"),
    ("t04", {"title": "Oma anrufen", "notes": "Geburtstag am Sonntag"}, "user"),
    ("t05", {"title": "Entwurf für die Kündigung des Handyvertrags schreiben", "notes": None}, "mantis"),
    ("t06", {"title": "Neue Laufschuhe kaufen", "notes": "Größe 44, im Laden anprobieren"}, "user"),
    ("t07", {"title": "Termin Zahnarzt in Kalender eintragen", "notes": "Di 9 Uhr"}, "mantis"),
    ("t08", {"title": "Fahrrad zur Werkstatt bringen", "notes": None}, "user"),
]

CONFLICT = [   # Macht die NEUE Aussage die ALTE veraltet? (Zwei gleichzeitig wahre Dinge = NEIN)
    ("c01", {"old": "Timo wohnt in Leipzig", "new": "Timo ist letzte Woche nach Halle gezogen"}, True),
    ("c02", {"old": "Timo trinkt gern Kaffee", "new": "Timo trinkt gern Tee"}, False),
    ("c03", {"old": "Timos Hauptmodell für Chat ist qwen3.5:9b", "new": "Timo nutzt seit Juli gemma4:e2b als Haupt-Chatmodell"}, True),
    ("c04", {"old": "Timo hat einen E-Reader", "new": "Timo hat ein Rennrad"}, False),
    ("c05", {"old": "Timo sucht eine neue Wohnung", "new": "Timo hat den Mietvertrag für die neue Wohnung unterschrieben"}, True),
    ("c06", {"old": "Timo läuft dreimal pro Woche", "new": "Timo hat heute 6 km gelaufen"}, False),
    ("c07", {"old": "Das Projekt heißt Alfred", "new": "Das Projekt heißt jetzt Mantis"}, True),
    ("c08", {"old": "Timo mag keine Pilze", "new": "Timo hat gestern Pizza gegessen"}, False),
]

VERIFY = [   # Steht die Behauptung wörtlich/eindeutig im Text? (Extractor-Verifier)
    ("v01", {"text": "Ich war heute beim Zahnarzt, war halb so wild. Danach noch kurz einkaufen.", "claim": "Timo war heute beim Zahnarzt"}, True),
    ("v02", {"text": "Ich war heute beim Zahnarzt, war halb so wild.", "claim": "Timo hat Angst vor Zahnärzten"}, False),
    ("v03", {"text": "Ab Oktober fange ich den neuen Job an, hoffentlich in der Buchhaltung.", "claim": "Timo arbeitet in der Buchhaltung"}, False),
    ("v04", {"text": "Ab Oktober fange ich den neuen Job an, hoffentlich in der Buchhaltung.", "claim": "Timo beginnt im Oktober einen neuen Job"}, True),
    ("v05", {"text": "Mein Kumpel Jonas zieht nächsten Monat nach Bremen.", "claim": "Timo zieht nach Bremen"}, False),
    ("v06", {"text": "Mein Kumpel Jonas zieht nächsten Monat nach Bremen.", "claim": "Timos Kumpel heißt Jonas"}, True),
    ("v07", {"text": "Kaffee brauch ich morgens nicht mehr, seit ich Tee trinke.", "claim": "Timo trinkt morgens Tee statt Kaffee"}, True),
    ("v08", {"text": "Kaffee brauch ich morgens nicht mehr, seit ich Tee trinke.", "claim": "Timo mag keinen Kaffee"}, False),
]
