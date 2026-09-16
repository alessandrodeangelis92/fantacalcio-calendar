#!/usr/bin/env python3

import json
import logging
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar, Event, Alarm


CONFIG_FILE = Path("config.json")
OUTPUT_FILE = Path("fantacalcio.ics")

USER_AGENT = "fantacalcio-calendar-updater/1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

LOGGER = logging.getLogger(__name__)


def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"File non trovato: {CONFIG_FILE}"
        )

    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = json.load(file)

    required_fields = [
        "source_url",
        "calendar_name",
        "minutes_before_first_match",
        "season",
        "timezone",
    ]

    for field in required_fields:
        if field not in config:
            raise ValueError(
                f"Campo mancante in config.json: {field}"
            )

    return config


def download_feed(source_url):
    LOGGER.info(
        "Scaricamento feed: %s",
        source_url,
    )

    response = requests.get(
        source_url,
        headers={
            "User-Agent": USER_AGENT,
        },
        timeout=30,
    )

    response.raise_for_status()

    content = response.content

    if not content.strip():
        raise ValueError(
            "Il feed scaricato è vuoto"
        )

    if b"BEGIN:VCALENDAR" not in content:
        raise ValueError(
            "Il contenuto scaricato non sembra essere "
            "un calendario ICS valido"
        )

    LOGGER.info(
        "Feed scaricato: %d byte",
        len(content),
    )

    return content


def get_property(component, property_name):
    value = component.get(property_name)

    if value is None:
        return None

    try:
        return str(value)
    except Exception:
        return None


def normalize_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()


def extract_round_from_text(text):
    """
    Cerca indicazioni come:
    - Giornata 1
    - Round 1
    - Matchday 1
    - MD1
    """

    if not text:
        return None

    patterns = [
        r"\bgiornata\s*[-:]?\s*(\d{1,2})\b",
        r"\bround\s*[-:]?\s*(\d{1,2})\b",
        r"\bmatchday\s*[-:]?\s*(\d{1,2})\b",
        r"\bjornada\s*[-:]?\s*(\d{1,2})\b",
        r"\bmd\s*[-:]?\s*(\d{1,2})\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return int(match.group(1))

    return None


def extract_round(component):
    """
    Prova a ricavare il numero della giornata da diversi
    campi dell'evento ICS.
    """

    candidate_fields = [
        "X-ROUND",
        "X-MATCHDAY",
        "X-GAMEWEEK",
        "X-WEEK",
        "SUMMARY",
        "DESCRIPTION",
        "LOCATION",
        "CATEGORIES",
    ]

    for field in candidate_fields:
        value = component.get(field)

        if value is None:
            continue

        if isinstance(value, (list, tuple)):
            text = " ".join(
                str(item)
                for item in value
            )
        else:
            text = str(value)

        round_number = extract_round_from_text(text)

        if round_number is not None:
            return round_number

    return None


def parse_event_datetime(
    component,
    property_name,
    local_timezone,
):
    value = component.get(property_name)

    if value is None:
        return None

    dt = value.dt

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=local_timezone
            )

        return dt.astimezone(local_timezone)

    return None


def is_probable_match_event(component):
    """
    Filtra gli eventi che sembrano essere partite.
    """

    if component.name != "VEVENT":
        return False

    if component.get("DTSTART") is None:
        return False

    summary = normalize_text(
        get_property(component, "SUMMARY")
    )

    if not summary:
        return False

    excluded_terms = [
        "allenamento",
        "training",
        "meeting",
        "reminder",
        "scadenza formazione",
    ]

    summary_lower = summary.lower()

    if any(
        term in summary_lower
        for term in excluded_terms
    ):
        return False

    return True


def parse_matches(
    feed_content,
    local_timezone,
):
    calendar = Calendar.from_ical(feed_content)

    matches = []

    for component in calendar.walk():
        if not is_probable_match_event(component):
            continue

        start = parse_event_datetime(
            component,
            "DTSTART",
            local_timezone,
        )

        if start is None:
            continue

        summary = normalize_text(
            get_property(component, "SUMMARY")
        )

        description = normalize_text(
            get_property(component, "DESCRIPTION")
        )

        location = normalize_text(
            get_property(component, "LOCATION")
        )

        uid = normalize_text(
            get_property(component, "UID")
        )

        round_number = extract_round(component)

        matches.append(
            {
                "summary": summary,
                "description": description,
                "location": location,
                "uid": uid,
                "start": start,
                "round": round_number,
            }
        )

    if not matches:
        raise ValueError(
            "Nessuna partita trovata nel feed ICS"
        )

    matches.sort(
        key=lambda match: match["start"]
    )

    LOGGER.info(
        "Eventi trovati nel feed: %d",
        len(matches),
    )

    rounds_found = sorted(
        {
            match["round"]
            for match in matches
            if match["round"] is not None
        }
    )

    if rounds_found:
        LOGGER.info(
            "Giornate individuate nel feed: %s",
            rounds_found,
        )
    else:
        LOGGER.warning(
            "Il feed non espone una giornata "
            "riconoscibile negli eventi"
        )

    return matches


def group_matches_by_round(matches):
    """
    Raggruppa le partite per giornata.
    """

    grouped = defaultdict(list)
    without_round = []

    for match in matches:
        if match["round"] is None:
            without_round.append(match)
        else:
            grouped[match["round"]].append(match)

    return grouped, without_round


def group_unknown_matches_by_blocks(matches):
    """
    Fallback per feed che non riportano il numero
    della giornata.

    Viene utilizzato soltanto se il numero totale
    degli eventi è multiplo di 10.
    """

    if not matches:
        return {}

    if len(matches) % 10 != 0:
        raise ValueError(
            "Impossibile determinare le giornate: "
            "il feed non contiene il numero della giornata "
            "e il numero di partite non è un multiplo di 10."
        )

    grouped = defaultdict(list)

    for index, match in enumerate(matches):
        round_number = index // 10 + 1
        grouped[round_number].append(match)

    LOGGER.warning(
        "Giornate ricostruite per blocchi di 10 partite. "
        "Verificare il risultato."
    )

    return grouped


def validate_matches(grouped):
    if not grouped:
        raise ValueError(
            "Nessuna giornata disponibile"
        )

    for round_number, matches in grouped.items():
        if not matches:
            raise ValueError(
                f"La giornata {round_number} "
                "non contiene partite"
            )

        matches.sort(
            key=lambda match: match["start"]
        )

        first_match = matches[0]["start"]
        last_match = matches[-1]["start"]

        LOGGER.info(
            "Giornata %s: %d partite, prima %s, ultima %s",
            round_number,
            len(matches),
            first_match.isoformat(),
            last_match.isoformat(),
        )


def make_uid(round_number, season):
    """
    Genera un UID stabile e non riconducibile
    al nome dell'utente.
    """

    safe_season = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        season,
    ).strip("-")

    return (
        f"fantacalcio-formazione-{safe_season}"
        f"-giornata-{round_number}@calendar.local"
    )


def add_alarm(event, trigger):
    """
    Aggiunge un avviso all'evento.

    trigger = timedelta(0) significa:
    avviso esattamente all'orario dell'evento.
    """

    alarm = Alarm()

    alarm.add(
        "ACTION",
        "DISPLAY",
    )

    alarm.add(
        "DESCRIPTION",
        "Scadenza formazione Fantacalcio",
    )

    alarm.add(
        "TRIGGER",
        trigger,
    )

    event.add_component(alarm)


def create_calendar(
    config,
    grouped_matches,
):
    calendar = Calendar()

    # Identificativo generico, senza nome o username personale.
    calendar.add(
        "PRODID",
        "-//Fantacalcio Calendar//Calendar Generator//IT",
    )

    calendar.add(
        "VERSION",
        "2.0",
    )

    calendar.add(
        "CALSCALE",
        "GREGORIAN",
    )

    calendar.add(
        "METHOD",
        "PUBLISH",
    )

    calendar.add(
        "X-WR-CALNAME",
        config["calendar_name"],
    )

    calendar.add(
        "X-WR-CALDESC",
        "Scadenze per inserire la formazione Fantacalcio",
    )

    calendar.add(
        "X-WR-TIMEZONE",
        config["timezone"],
    )

    season = config["season"]

    minutes_before = int(
        config["minutes_before_first_match"]
    )

    generated_at = datetime.now(timezone.utc)

    for round_number in sorted(grouped_matches):
        matches = grouped_matches[round_number]

        matches.sort(
            key=lambda match: match["start"]
        )

        first_match = matches[0]["start"]

        # L'evento viene fissato 60 minuti prima
        # della prima partita, in base al valore
        # configurato in config.json.
        deadline = first_match - timedelta(
            minutes=minutes_before
        )

        event = Event()

        event.add(
            "UID",
            make_uid(
                round_number,
                season,
            ),
        )

        event.add(
            "DTSTAMP",
            generated_at,
        )

        event.add(
            "DTSTART",
            deadline,
        )

        event.add(
            "DTEND",
            deadline + timedelta(minutes=5),
        )

        event.add(
            "SUMMARY",
            (
                "⚽ Inserire formazione Fantacalcio"
                f" — Giornata {round_number}"
            ),
        )

        match_lines = []

        for match in matches:
            match_lines.append(
                f"- {match['start'].strftime('%d/%m/%Y %H:%M')} "
                f"{match['summary']}"
            )

        description = (
            f"Scadenza consigliata per inserire la formazione "
            f"della giornata {round_number}.\n\n"
            f"La prima partita inizia alle "
            f"{first_match.strftime('%d/%m/%Y alle %H:%M')}.\n"
            f"L'evento è impostato a "
            f"{minutes_before} minuti prima.\n\n"
            f"Partite rilevate:\n"
            + "\n".join(match_lines)
        )

        event.add(
            "DESCRIPTION",
            description,
        )

        event.add(
            "CATEGORIES",
            "Fantacalcio",
        )

        event.add(
            "STATUS",
            "CONFIRMED",
        )

        event.add(
            "TRANSP",
            "TRANSPARENT",
        )

        # Un solo avviso, esattamente all'orario
        # dell'evento.
        add_alarm(
            event,
            timedelta(0),
        )

        calendar.add_component(event)

    return calendar


def write_calendar(
    calendar,
    output_file,
):
    """
    Scrive prima in un file temporaneo e poi sostituisce
    il file finale.

    Se la generazione fallisce, il calendario precedente
    non viene perso.
    """

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=output_file.parent,
        prefix=".fantacalcio-",
        suffix=".ics",
    ) as temporary_file:
        temporary_path = Path(
            temporary_file.name
        )

        temporary_file.write(
            calendar.to_ical()
        )

    os.replace(
        temporary_path,
        output_file,
    )

    LOGGER.info(
        "Calendario scritto in: %s",
        output_file,
    )


def main():
    try:
        config = load_config()

        local_timezone = ZoneInfo(
            config["timezone"]
        )

        feed_content = download_feed(
            config["source_url"]
        )

        matches = parse_matches(
            feed_content,
            local_timezone,
        )

        grouped, without_round = group_matches_by_round(
            matches
        )

        if without_round:
            LOGGER.warning(
                "Eventi senza numero di giornata: %d",
                len(without_round),
            )

        if not grouped:
            grouped = group_unknown_matches_by_blocks(
                matches
            )

        elif without_round:
            LOGGER.warning(
                "Gli eventi senza giornata verranno ignorati "
                "per evitare assegnazioni errate."
            )

        validate_matches(grouped)

        calendar = create_calendar(
            config,
            grouped,
        )

        write_calendar(
            calendar,
            OUTPUT_FILE,
        )

        LOGGER.info(
            "Generazione completata correttamente"
        )

    except Exception as error:
        LOGGER.error(
            "Generazione fallita: %s",
            error,
        )

        sys.exit(1)


if __name__ == "__main__":
    main()