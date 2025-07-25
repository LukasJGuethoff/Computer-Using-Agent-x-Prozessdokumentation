# Computer-Using-Agent-x-Prozessdokumentation

Dies ist die Implementation eines Computer Using Agents (CUAs) basierend auf der Anthropic API erweitert um Prozessdokumentation. Diese kann in textueller oder graph Form übergeben werden. Es soll geprüft werden, ob CUAs mit Prozessdokumentationen eine besserer Performance aufweisen als ohne.
Im Ordner Tests befinden sich Testfälle. Dieses Repo wurde im Rahmen des Models 'Proseminar' des Informatik Bachelor Studiums der Universität Paderborn entwickelt.

Diese Implementierung ist auf dem Betriebsystem Windows 11 erfolgt.

## 1. Neo4j installieren

Instelliere die neuste Community-Version
```bash
https://neo4j.com/deployment-center/
```
Starte den Server
Lege eine lokale DB an. Speichere das vergebene Passwort in pw.txt.
Bolt‑Endpunkt:
bolt://localhost:7687   # User: neo4j

> **Hinweis:** Wenn `--graph-file` verwendet wird, muss Neo4j währenddessen aktiv sein.


## 2. requirements.txt installieren
```bash
pip install -r requirements.txt
```

## 3. Anthropic API-Key

Falls noch nicht vorhanden muss ein Anthropic API-Key generiert werden.
```bash
https://console.anthropic.com/
```
Dieser muss in key.txt gespeichert werden.
Es empfielt sich sehr stark auf 'Tier 2' der API upzugraden, da ansonsten es zu Performance Problemen aufgrund eines zu geringen 'Maximum input tokens per minute (ITPM)' kommen wird.


## 4. Beispiele zur Nutzung
Beispielhafter Aufruf ohne Prozessdokumentation:
```bash
python agent.py -api key.txt -p tests/Grammatiktest_Absolvieren/Nutzeraufforderung.txt
```

Beispielhafter Aufruf mit textueller Prozessdokumentation:
```bash
python agent.py -api key.txt -p tests/Grammatiktest_Absolvieren/Nutzeraufforderung.txt -t tests/Grammatiktest_Absolvieren/text_Prozess_doku.txt
```

Beispielhafter Aufruf mit grafischer Prozessdokumentation:
```bash
python agent.py -api key.txt -pw pw.txt -p tests/Grammatiktest_Absolvieren/Nutzeraufforderung.txt -g tests/Grammatiktest_Absolvieren/Graph_Prozess_doku.yaml
```
In der Implementation wird davon ausgegangen, dass ein Browser Fenster bereits geöffnet ist, und die Bildschirmauflösung auf 1280 x 800 eingestellt ist.

API-Fehler wie 'overloaded' o. Ä. werden bewusst nicht gecatchet, um den Benutzer in Kenntnis zu setzen.