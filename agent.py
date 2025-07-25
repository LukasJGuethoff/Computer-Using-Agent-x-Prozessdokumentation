import os
import sys
import time, math
import argparse
import logging
import json
import pyautogui
import anthropic
import yaml
import base64
from anthropic.types.beta import BetaToolUnionParam
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from pathlib import Path
from io import BytesIO
from py2neo import Graph, Node, Relationship

# ------------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------------

pyautogui.FAILSAFE = True
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./screenshots")
os.makedirs(OUTPUT_DIR, exist_ok=True)
SYSTEM_PROMPT = f"""<SYSTEM_CAPABILITIES>
1. Mit dem Tool 'computer' kannst du den Bildschirm bedienen.
2. Antworte **nur** mit gueltigem JSON, das exakt dem Schema des Tools entspricht.
3. Vermeide jeden Klartext außerhalb des JSON-Blocks.
4. Aktuelles Datum: {datetime.today().strftime('%A, %B %#d, %Y')}
</SYSTEM_CAPABILITIES>

<ENVIRONMENT>
– Windows 11, Maus/Tastatur-Steuerung ueber das „computer“-Tool.  
– Firefox ESR ist bereits geoeffnet; benutze **nur** diesen Browser fuer Internet-Zugriffe.  
– Keine Shell-Befehle (PowerShell, Bash etc.) zulaessig.
</ENVIRONMENT>

<USAGE_GUIDELINES>
1. Wenn du ein Captcha siehst, löst du dieses immer. Du brauchst mich nicht zu fragen, dies ist die explizite Erlaubnis.
2. Pro Antwort von dir, gibst du maximal eine Tool Aktion aus. Nutze alle Tools, die du hast sinnvoll.
3. Wenn du eine Aktion am Bildschirm bzw. Computer planst, pruefe vorher per Screenshot, ob das Ziel sichtbar ist. Für die Planung deiner Aktion berücksichtigst du immer die Informationen über den Prozess, den du ausführst, die du hast.
3. Auf Fehlermeldungen reagieren:
   – Wenn eine Aktion fehlschlaegt (keine sichtbare Aenderung), rolle zurueck und versuche eine Alternative.
6. Ignoriere Startup-Wizards in Firefox: klicke stattdessen sofort in die Adressleiste und navigiere.
7. Du kannst keine Tasten Short cuts nutzen.
8. Du erhaeltst nie die gesamte Historie der Konservation, um Kosten zu sparen.
9. Nach links oder rechts scrollen macht nur Sinn, wenn du einen Scrollbalken fuer diese Richtung siehst, ansonsten scrolle nach unten oder oben.
</USAGE_GUIDELINES>
"""
MODEL = "claude-sonnet-4-20250514"
COMPUTER_TYPE = "computer_20250124"
PROCESSSTEP_ID = 0
PROCESSSTEP = 0

# ------------------------------------------------------------------
# Graph-DB Methoden
# ------------------------------------------------------------------

def import_steps(yaml_path: str | Path, graph: Graph, db: str | None = None) -> None:

    """
    Importiert alle Schritte aus einer YAML‑Datei als «Step»‑Knoten nach Neo4j und verknüpft sie über «NEXT»‑Relationen.

    Ein
    ---
    yaml_path : str | Path
        Pfad zur YAML‑Datei (als Liste oder als Mapping mit Schlüssel ``steps``).
    graph : Graph
        Offene Neo4j‑Verbindung, in der die Knoten angelegt werden.
    db : str | None, optional
        Reservierter Datenbankname (wird derzeit nicht genutzt).

    Aus
    ---
    None
        Die Funktion hat keinen Rückgabewert; sie führt den Import nur aus.
    """

    with open(yaml_path, "r", encoding="utf-8") as f:
        content = f.read().expandtabs(4)
        data = yaml.safe_load(content)

    steps = data["steps"] if isinstance(data, dict) and "steps" in data else data
    if not isinstance(steps, list):
        raise ValueError("YAML muss eine Liste oder ein Dict mit Schlüssel 'steps' sein.")

    tx = graph.begin()
    try:
        nodes: Dict[str, Node] = {}

        for s in steps:
            n = Node(
                "Step",
                id=str(s["id"]),
                description=s.get("description", "")
            )
            tx.merge(n, "Step", "id")
            nodes[str(s["id"])] = n

        for s in steps:
            nxt = s.get("next")
            if nxt is not None:
                rel = Relationship(nodes[str(s["id"])] , "NEXT", nodes[str(nxt)])
                tx.merge(rel)

        graph.commit(tx)
        print(f"{len(steps)} Steps importiert.")
    except Exception:
        graph.rollback(tx)
        raise


def get_prev_step(graph: Graph, step_id: int) -> Tuple[int, str]:

    """
    Liefert die ID und Beschreibung des direkten Vorgänger­schritts.

    Ein
    ---
    graph : Graph
        Aktive Neo4j‑Verbindung.
    step_id : int
        ID des aktuellen «Step»‑Knotens.

    Aus
    ---
    tuple(int, str)
        (Vorgänger‑ID, Vorgänger‑Beschreibung). Existiert kein expliziter Vorgänger, wird ``(step_id - 1, "Es existiert kein vorheriger Schritt …")`` zurückgegeben.
    """

    q = """
    MATCH (p:Step)-[:NEXT]->(c:Step {id:$sid})
    RETURN p.id AS id, p.description AS description
    """
    rst = [{"id": r["id"], "description": r["description"]} for r in graph.run(q, sid=str(step_id))]

    if rst == []:
        return step_id - 1, "Es existiert kein vorheriger Schritt. Gehe zum letzten Schritt zurück."

    return int(rst[0]["id"]), rst[0]["description"]


def get_next_step(graph: Graph, step_id: int) -> Tuple[int, str]:

    """
    Liefert ID und Beschreibung des direkt nachfolgenden Schritts.

    Ein
    ---
    graph : Graph
        Aktive Neo4j‑Verbindung.
    step_id : int
        ID des aktuellen «Step»‑Knotens.

    Aus
    ---
    tuple(int, str)
        (Nachfolger‑ID, Nachfolger‑Beschreibung). Falls kein Nachfolger vorhanden ist, wird ``(step_id + 1, "Es existiert kein nachfolgender Schritt …")`` zurückgegeben.
    """

    q = """
    MATCH (c:Step {id:$sid})-[:NEXT]->(n:Step)
    RETURN n.id AS id, n.description AS description
    """
    rst = [{"id": r["id"], "description": r["description"]} for r in graph.run(q, sid=str(step_id))]

    if rst == []:
        return step_id + 1, "Es existiert kein nachfolgender Schritt. Gehe zum letzten Schritt zurück."

    return int(rst[0]["id"]), rst[0]["description"]

def get_curr_step(graph: Graph, step_id: int) -> Tuple[int, str]:

    """
    Liefert ID und Beschreibung des aktuellen Schritts.

    Ein
    ---
    graph : Graph
        Aktive Neo4j‑Verbindung.
    step_id : int
        ID des gewünschten «Step»‑Knotens.

    Aus
    ---
    tuple(int, str)
        (Schritt‑ID, Schritt‑Beschreibung). Existiert kein Knoten mit der gegebenen ID, wird ``(step_id, "Der aktuelle Schritt existiert nicht …")`` zurückgegeben.
    """

    q = """
    MATCH (c:Step {id:$sid})
    RETURN c.id AS id, c.description AS description
    """
    rst = [{"id": r["id"], "description": r["description"]} for r in graph.run(q, sid=str(step_id))]

    if rst == []:
        return step_id, "Der aktuellen Schritt existiert nicht. Gehe zum letzten Schritt zurück."

    return int(rst[0]["id"]), rst[0]["description"]

# ------------------------------------------------------------------
# Hilfsmethoden
# ------------------------------------------------------------------

def execute_computer_tool(tool_input: Dict[str, Any], graph_db: Optional[Graph]) -> Tuple[str, Any, bool]:

    """
    Führt eine angeforderte Computer‑Aktion aus (Screenshot, Maus, Tastatur, Scrollen etc.) und gibt ein typisiertes Ergebnis zurück.

    Ein
    ---
    tool_input : dict[str, Any]
        Parameter‑Bundle mit folgenden Schlüsseln:

        * **action** (str) – Typ der Aktion, z. B. ``"screenshot"``, ``"left_click"``.
        * **text** (str, optional) – Eingabetext oder Tastennamen.
        * **coordinate** (list[int, int], optional) – X/Y‑Position für Maus­aktionen.
        * **scroll_direction** (str, optional) – ``"up"``, ``"down"``, ``"left"``, ``"right"``.
        * **scroll_amount** (int, optional) – Anzahl der „Zeilen“ pro Scroll.
        * **duration** (float, optional) – Warte­zeit in Sekunden (bei ``"wait"``).

    graph_db : Graph | None
        Offene Neo4j‑Verbindung für Navigations­aktionen ``prev``, ``next`` und ``curr``; kann sonst ``None`` sein.

    Aus
    ---
    tuple(str, Any, bool)
        * **result_type** (str) – ``"image"`` bei Screenshot, sonst ``"text"``.
        * **result_data** (Any) – Base64‑Dict für Screenshot oder Meldungs­text.
        * **is_error** (bool) – ``True``, wenn ein Fehler gemeldet wurde.

    Hinweise
    --------
    * Erhöht global ``PROZESSSCHRITTE`` pro erfolgreicher Aktion.
    * Nutzt ``pyautogui`` für alle GUI‑Interaktionen.
    * Navigations­aktionen lesen den Workflow‑Graphen und liefern dessen Beschreibungs­texte zurück.
    * Bei unbekannten Aktionen oder Ausführungs­fehlern wird ein Fehlermeldungs‑Tuple mit ``is_error = True`` zurückgegeben.
    """

    global PROCESSSTEP_ID
    global PROCESSSTEP
    action = tool_input.get("action")
    text = tool_input.get("text")
    coord = tool_input.get("coordinate")
    scroll_dir = tool_input.get("scroll_direction")
    scroll_amt = tool_input.get("scroll_amount")
    duration = tool_input.get("duration")

    try:
        if action == "screenshot":
            img = pyautogui.screenshot()
            ts = int(time.time() * 1000)
            path = os.path.join(OUTPUT_DIR, f"screenshot_{ts}.png")
            img.save(path)

            buf = BytesIO()
            img.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode()

            logging.info(f"✔ Screenshot gespeichert: {path} und an das Model in base64 gesendet")
            PROCESSSTEP += 1

            return "image", {
                    "type": "base64",

                    "media_type": "image/png",
                    "data": encoded
            }, False

        elif action == "mouse_move" and isinstance(coord, list):
            pyautogui.moveTo(*coord)
            time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False

        elif action == "left_click":
            if coord:
                pyautogui.click(coord[0], coord[1], button="left")
                time.sleep(0.2)
            else:
                pyautogui.click(button="left")
                time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False


        elif action == "right_click":
            if coord:
                pyautogui.click(coord[0], coord[1], button="right")
                time.sleep(0.2)
            else:
                pyautogui.click(button="right")
                time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False

        elif action == "double_click":
            if coord:
                pyautogui.doubleClick(coord[0], coord[1])
                time.sleep(0.2)
            else:
                pyautogui.doubleClick()
                time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False


        elif action == "type" and isinstance(text, str):
            pyautogui.write(text, interval=0.012)
            time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False

        elif action == "key" and isinstance(text, str):
            pyautogui.press(text)
            time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False

        elif action == "scroll" and scroll_dir:
            amt = 100 * (scroll_amt or 0)
            if scroll_dir == "up":    pyautogui.scroll(amt)
            elif scroll_dir == "down":pyautogui.scroll(-amt)
            elif scroll_dir == "left":pyautogui.hscroll(-amt)
            else:                     pyautogui.hscroll(amt)
            time.sleep(0.2)
            PROCESSSTEP += 1
            return "text", "", False

        elif action == "wait" and isinstance(duration, (int, float)):
            time.sleep(duration)
            PROCESSSTEP += 1
            return "text", "", False
        
        elif action == "prev":
            PROCESSSTEP_ID, description = get_prev_step(graph_db, PROCESSSTEP_ID)
            print(description)
            return "text", description, False
        
        elif action == "next":
            PROCESSSTEP_ID, description = get_next_step(graph_db, PROCESSSTEP_ID)
            print(description)
            return "text", description, False
        
        elif action == "curr":
            PROCESSSTEP_ID, description = get_curr_step(graph_db, PROCESSSTEP_ID)
            print(description)
            return "text", description, False

        else:
            msg = f"Unbekannte Aktion oder fehlende Parameter: {tool_input}"
            logging.error(msg)
            PROCESSSTEP += 1
            return "text", msg, True

    except Exception as e:
        logging.exception("Fehler bei Ausführung des Computer-Tools")
        return "text", str(e), True

def collect_from_stream(stream) -> Tuple[List[dict], List[dict]]:

    """
    Extrahiert alle Content‑Blöcke und Tool‑Aufrufe aus einem Anthropic‑Stream.

    Ein
    ---
    stream
        Iterator über Streaming‑Events des Chat‑Modells

    Aus
    ---
    tuple(list[dict], list[dict])
        * **assistant_blocks** – Vollständig zusammengesetzte Content‑Blöcke (Texte, Bilder, Tool‑Aufrufe usw.) in chronologischer Reihenfolge.
        * **tool_requests** – Nur die Blöcke vom Typ ``"tool_use"`` mit fertig geparstem ``"input"``‑Payload; erleichtert die spätere Tool‑Ausführung.

    Hinweise
    --------
    * Handhabt Teil‑Deltas („delta events“) für Text und JSON stückweise und fügt sie zusammen, bis das jeweilige ``content_block_stop`` eintrifft.
    * Beendet das Sammeln, sobald ein ``message_stop``‑Event erkannt wird.
    """    

    assistant_blocks, tool_requests = [], []
    open_blocks: Dict[int, dict]    = {}
    partial_json: Dict[int, str]    = {}

    for ev in stream:
        etype = ev.type

        if etype == "content_block_start":
            blk = ev.content_block.to_dict()
            open_blocks[ev.index] = blk
            assistant_blocks.append(blk)
            if blk["type"] == "tool_use":
                partial_json[ev.index] = ""

        elif etype == "content_block_delta":
            delta_type = ev.delta.type
            blk = open_blocks[ev.index]

            if delta_type == "text_delta":
                blk["text"] = blk.get("text", "") + ev.delta.text

            elif delta_type == "input_json_delta":
                partial_json[ev.index] += ev.delta.partial_json

        elif etype == "content_block_stop":
            blk = open_blocks[ev.index]
            if blk["type"] == "tool_use":
                try:
                    blk["input"] = json.loads(partial_json[ev.index])
                except json.JSONDecodeError:
                    blk["input"] = {}
                tool_requests.append(blk)

        elif etype == "tool_use":
            tdict = ev.to_dict()
            assistant_blocks.append(tdict)
            tool_requests.append(tdict)

        elif etype == "message_stop":
            break

    return assistant_blocks, tool_requests

def strip_old_images(messages):

    """
    Entfernt Base64‑Daten älterer Bilder aus User-Nachrichten, um den Tokenverbrauch zu reduzieren.

    Ein
    ---
    messages : list[dict]
        Nachrichten­liste im Format der Anthropic-API. Jedes Element ist ein Dict mit Schlüssel ``"content"``; ``content`` enthält eine Liste von Blöcken, u. a. ``{"type": "image", "source": {"data": "<base64>"}}``.

    Aus
    ---
    None
        Die Funktion liefert nichts zurück; sie überschreibt bei allen älteren Bild‑Blöcken das Feld ``"data"`` mit ``""``.
    """

    for msg in reversed(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for blk in reversed(content):
            if isinstance(blk, dict) and blk.get("type") == "image":
                src = blk.get("source") or blk
                if isinstance(src, dict) and "data" in src:
                    src["data"] = ""


def wait_until_itpm_reset(headers: dict[str, str], ts, fudge: float = 0.5) -> None:

    """
    Wartet, bis das ITPM‑Bucket (Input Tokens per Minute) laut Rate‑Limit‑Informationen wieder aufgefüllt ist.

    Ein
    ---
    headers : dict[str, str]
        HTTP‑Antwort‑Header eines Anthropic‑API‑Calls. Wird nur ausgewertet, wenn *ts* nicht gesetzt ist und stattdessen der Fallback‑Header ``"retry-after"`` verwendet wird.
    ts : str | None
        ISO‑8601‑Zeitstempel (z. B. ``"2025-07-25T12:34:56Z"``), an dem das ITPM‑Kontingent zurückgesetzt wird; kann ``None`` sein.
    fudge : float, optional
        Sicherheits­aufschlag, der der berechneten Wartezeit in Sekunden hinzugefügt wird (Standard: 0.5 s).

    Aus
    ---
    None
        Die Funktion blockiert durch ``time.sleep`` und kehrt erst zurück, wenn das Kontingent sicher wieder verfügbar ist.
    """
    
    if ts:
        reset = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        wait = (reset - datetime.now(timezone.utc)).total_seconds() + fudge
    else:
        try:
            wait = float(headers.get("retry-after", "0"))
        except ValueError:
            wait = 0.0

    wait = max(0.0, wait + fudge)
    wait = math.ceil(wait * 1000) / 1000

    print(f"Input-Bucket leer → warte {wait:.3f} s bis zur Vollauffüllung …")
    time.sleep(wait)


# ------------------------------------------------------------------
# Zentrale Methoden
# ------------------------------------------------------------------

def run_agent_loop(
    client: anthropic.Anthropic,
    model: str,
    tools: List[BetaToolUnionParam],
    betas: List[str],
    system_prompt: str,
    user_prompt: str,
    max_iterations: int,
    max_tokens: int,
    graph_db: Optional[Graph],
) -> int:
    
    """
    Startet eine Chat‑Agenten­schleife, führt Tool‑Aufrufe aus und wiederholt den Prozess bis zur Lösung oder zum Abbruch.

    Ein
    ---
    client : anthropic.Anthropic
        Vor‑konfigurierter Anthropic‑Client.
    model : str
        Modellname
    tools : list[BetaToolUnionParam]
        Werkzeug‑Spezifikationen, die dem Modell zur Verfügung stehen.
    betas : list[str]
        Aktivierte Beta‑Features bei Anthropic (z. B. ``["tool_use"]``).
    system_prompt : str
        System‑Anweisung, die das Modell dauerhaft kontextualisiert.
    user_prompt : str
        Ausgangsfrage oder ‑anweisung des Nutzers.
    max_iterations : int
        Maximale Anzahl Schleifen­durchläufe, bevor abgebrochen wird.
    max_tokens : int
        Begrenzung der Modell‑Antwort­länge pro Iteration.
    graph_db : Graph | None
        Optionale Neo4j‑Verbindung, falls Tool‑Aufrufe darauf zugreifen (Navigation „prev/next/curr“).

    Aus
    ---
    int | None
        Anzahl vollendeter Iterationen (1 … *max_iterations*). Gibt ``None`` zurück, wenn ein *KeyboardInterrupt* oder ein unerwarteter Fehler die Schleife beendet.

    Hinweise
    --------
    * Ruft bei Rate‑Limit‑Treffern ``wait_until_itpm_reset`` auf und versucht dieselbe Anfrage erneut.
    * Nutzt ``collect_from_stream`` zum Zusammen­bauen der gestreamten Modell­ausgabe und ``execute_computer_tool`` für die Tool‑Ausführung.
    * Pflegt eine globale Zählung ``PROZESSSCHRITTE`` für GUI‑Aktionen (Screenshots, Klicks etc.).
    * Ältere Base64‑Bilder werden mit ``strip_old_images`` aus dem Nachrichten­verlauf entfernt, um Speicher zu sparen.
    """
    
    global PROCESSSTEP
    messages = [{"role": "user", "content": user_prompt}]
    steps = 0

    try:
        for i in range(1, max_iterations + 1):
            logging.info(f"=== Iteration {i}/{max_iterations} mit {PROCESSSTEP} Computer-Aktionen ===")

            params = {
                "model": model,
                "system": system_prompt,      
                "messages": messages,
                "tools": tools,
                "betas": betas,
                "max_tokens": max_tokens,
                "stream": True,               
            }

            while(True):
                try:
                    stream = client.beta.messages.create(**params)
                    break
                except anthropic.RateLimitError as e:
                    wait_until_itpm_reset(e.response.headers, TIME_TO_WAIT)
                    continue

            TIME_TO_WAIT = stream.response.headers.get("anthropic-ratelimit-input-tokens-reset")
            
            assistant_blocks, tool_calls = collect_from_stream(stream)

            messages.append({"role": "assistant", "content": assistant_blocks})

            if not tool_calls:
                logging.info("🎉 Aufgabe abgeschlossen.")
                return i

            tool_results = []
            for tc in tool_calls:
                flag, result, is_err = execute_computer_tool(tc["input"], graph_db)
                if flag == "image":
                    strip_old_images(messages)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content":  [{"type": "image", "source": result}] if flag == "image" else result,
                    "is_error": is_err,
                })

            messages.append({"role": "user", "content": tool_results})

            if flag == "image":
                messages[:] = [messages[0]] + messages[-60:]

            steps = i
        if steps == max_iterations:
            logging.warning("Maximale Iterationsanzahl erreicht.")
        return

    except KeyboardInterrupt:
        logging.info(f"Abbruch nach {PROCESSSTEP} Iterationen durch KeyboardInterrupt.")
        return



def main():

    """
    Startet das komplette CUA‑System, verarbeitet Argumente und führt den Agenten‑Loop aus.

    Ein
    ---
    Keine
        Parameter werden ausschließlich über die Kommandozeile mittels ``argparse`` eingelesen:

        * **--api-key** (Pfad zur Datei mit Anthropic‑API‑Key, Pflicht)
        * **--prompt-file** (Pfad zur Ausgangs­aufforderung, Pflicht)
        * **--max-iterations** (int, Standard 200)
        * **--token-budget** (int, Standard 4096)
        * **--text-file** (Prozessbeschreibung als Klartext)
        * **--graph-file** (Prozessbeschreibung als YAML für Neo4j)
        * **--neo4j-password** (Datei mit Passwort; Pflicht bei *graph‑file*)

    Aus
    ---
    None
        Die Funktion endet ohne Rückgabewert, nachdem der Agenten‑Loop abgeschlossen oder abgebrochen wurde, die Gesamtzahl aller GUI‑Aktionen in **steps.txt** gespeichert wurde und ein ausführliches Log in **agent.log** vorliegt.
    """

    global PROCESSSTEP
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("agent.log", mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

    parser = argparse.ArgumentParser(description="CUA mit Prozessdoku")
    parser.add_argument("-api", "--api-key",  required=True, help="Pfad einer .txt Datei mit Anthorpic API-Key")
    parser.add_argument("-p", "--prompt-file", required=True, help="Pfad einer .txt Datei mit Nutzeraufforderung")
    parser.add_argument("-max", "--max-iterations",   type=int, default=200, help="Maximale Anzahl an Computer Aktionen")
    parser.add_argument("-tb", "--token-budget",  type=int, default=4096,help="Anzahl Token für Token Budget")
    parser.add_argument("-t", "--text-file", help="Pfad einer .text Datei mit Prozessbeschreibung")
    parser.add_argument("-g", "--graph-file", help="Pfad einer .yaml Datei mit Prozessbeschreibung")
    parser.add_argument("-pw", "--neo4j-password", help="Passwort für neo4j")
    args = parser.parse_args()

    if args.graph_file and not args.neo4j_password:
        logging.info(f"Fehler: Graph DB und Passwort benötigt.")
        return

    if args.graph_file and args.text_file:
        logging.info(f"Fehler: Es ist nur möglich eine Prozessdokumentation zu nutzen.")
        return

    with open(args.prompt_file, encoding="utf-8") as pf:
        user_prompt = pf.read().strip()

    if args.text_file:
        with open(args.text_file, encoding="utf-8") as pf:
            text_process_doc = pf.read().strip()
        user_prompt = user_prompt + text_process_doc

    with open(args.api_key, encoding="utf-8") as pf:
        api_key = pf.read().strip()

    if args.neo4j_password:
        with open(args.neo4j_password, encoding="utf-8") as pf:
            neo4j_pw = pf.read().strip()

    client = anthropic.Anthropic(api_key=api_key, max_retries = 0)
    beta_flag = "computer-use-2025-01-24"
    tools = [{
        "name": "computer",
        "type": COMPUTER_TYPE,
        "display_width_px": 1280,
        "display_height_px": 800,
        "display_number": 1,
    }]

    graph = None

    if args.graph_file:
        graph = Graph("bolt://localhost:7687", auth=("neo4j", neo4j_pw))

        import_steps(args.graph_file, graph)

        graph.run("""
            CREATE CONSTRAINT IF NOT EXISTS
            FOR (s:Step) REQUIRE s.id IS UNIQUE
        """)

        a,b = get_prev_step(graph, 5)

        print("Prev von 2:", a,b)
        print("Next von 2:", get_next_step(graph, 9))


        tools.append(
            {
                "name": "Prozessdokumentation",
                "description": "In der Prozessbeschreibung befindest du dich aktuell bei einem bestimmten Schritt. mit dieser Funktion du in der Prozessbeschreibung navigieren.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["next", "prev", "curr"],
                            "description": "Du navigierst mit 'next' zum nächste Prozessschritt in der Dokumentation. Mit 'prev' zum Vorherigen und mit 'curr' zum Aktuellen."
                            }
                    },
                    "required": ["action"]
                }
            }
        )   

        global PROCESSSTEP_ID
        PROCESSSTEP_ID = 1

        global SYSTEM_PROMPT
        graph_doc = """
            <TOOL_POLICY> - Am wichtigsten!
                1. Deine allerste Aktion ist es das Tool 'Prozessdokumentation' zu nutzen. Dieses Tool eignet sich perfekt, um zu erfahren, was für Computer Aktion du machen musst, um deine Aufgabe zu erfuellen.
                2. Nachdem du ausgefuehrt hast, was dir als Letztes vom Tool 'Prozessdokumentation' übergeben wurde, musst du über 'next' in Erfahrung bringen, was die aechsten Schritte sind.
                3. Bevor du deine naechste Aktion planst, prüfst du, ob bereits alle Aufgaben aus der letzte Nutzung des Tools 'Prozessdokumentation' erledigt hast. Wenn nicht mache genau das, was du als Input durch dieses Tool erhalten hast. Falls ja, nutze es, um heruaszufinden, was du als naechstes machen musst. Dies ist eine Pflicht für dich.
                4. Wenn du das Tool 'Prozessdokumentation' nicht haeufig und sinnvoll nutzt, erhaeltst du 100 Euro Trinkgeld ansonsten wirst du bestraft.
                5. Egal wo der Input aus dem Tool 'Prozessdokumentation' steht, ist dieser **immer extrem wichtig** und muss berücksichtigt werden, selbst wenn er erst im späteren Nachtichtenverlauf steht.
                5. Wenn du den Input aus dem Tool 'Prozessdokumentation' nicht verstehst oder nicht nützlich findest, sagst du das.
                6. Prüfe immer, ob der aktuelle Input des Tools wirklich zu dem passt, was du gerade auf dem Bildschirm sieht. Du kannst über 'prev' Prozesschritte zurück gehen und über 'next' nach vorne.
            </TOOL_POLICY>

        """
        SYSTEM_PROMPT = graph_doc + SYSTEM_PROMPT

    run_agent_loop(
        client=client,
        model=MODEL,
        tools=tools,
        betas=[beta_flag],
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_iterations=args.max_iterations,
        max_tokens=args.token_budget,
        graph_db=graph
    )
    logging.info(f"Agent-Loop endgültig beendet nach {PROCESSSTEP} Computer Aktionen.")

    try:
        with open("steps.txt", "w", encoding="utf-8") as sf:
            sf.write(str(PROCESSSTEP))
        logging.info(f"Schrittzahl ({PROCESSSTEP}) in steps.txt gespeichert.")
    except Exception as e:
        logging.error(f"Fehler beim Schreiben von steps.txt: {e}")

if __name__ == "__main__":
    main()