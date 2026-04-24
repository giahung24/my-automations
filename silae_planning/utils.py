import json
import logging
import os
import requests
from dateutil import parser
import re
from datetime import datetime, timedelta


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SilaeSession:
    def __init__(self, username, password, base_url=None, verify_ssl=True, ca_bundle=None):
        if base_url is None:
            base_url = "https://user.fiteco.rhsuite.silae.fr"
        login_url = f"{base_url}/login"
        self.session = login_silae_portal(
            username,
            password,
            login_url,
            verify_ssl=verify_ssl,
            ca_bundle=ca_bundle,
        )
        self.base_url = base_url

    def get_planning_events(self, date_from=None, date_to=None, view="timelineWeek"):
        """
        Get planning events for a date range, default from today to next 6 days
        """
        return get_planning_events(
            self.session, date_from, date_to, view, self.base_url
        )

    def get_planning_resources(self):
        return get_planning_resources(self.session, self.base_url)


def login_silae_portal(
    username,
    password,
    login_url=None,
    verify_ssl=True,
    ca_bundle=None,
):
    session = requests.Session()

    # Configure SSL verification once at session level.
    # - verify_ssl=True + ca_bundle set: use custom CA bundle
    # - verify_ssl=True + no ca_bundle: use system cert store
    # - verify_ssl=False: disable verification (for troubleshooting only)
    if verify_ssl and ca_bundle:
        if os.path.exists(ca_bundle):
            session.verify = ca_bundle
        else:
            logger.warning(
                "SILAE_CA_BUNDLE path not found: %s. Falling back to system certificate store.",
                ca_bundle,
            )
            session.verify = True
    else:
        session.verify = bool(verify_ssl)

    if login_url is None:
        login_url = "https://user.fiteco.rhsuite.silae.fr/login"
    logger.info(f"Login URL: {login_url} with username: {username}")
    # Première requête pour récupérer les cookies et tokens CSRF si nécessaire
    try:
        response = session.get(login_url)
    except requests.exceptions.SSLError as exc:
        logger.error(
            "SSL verification failed while connecting to %s. "
            "Set SILAE_CA_BUNDLE to your corporate/root CA file, or set "
            "SILAE_VERIFY_SSL=false only for troubleshooting. Error: %s",
            login_url,
            exc,
        )
        return None

    # Utiliser regex pour extraire le token CSRF au lieu de BeautifulSoup
    csrf_token = None
    csrf_pattern = r'name="_csrf_token"[^>]*value="([^"]*)"'
    match = re.search(csrf_pattern, response.text)
    if match:
        csrf_token = match.group(1)

    if not csrf_token:
        logger.debug("Token CSRF non trouvé")
        return None

    # Données de connexion avec le token CSRF
    login_data = {
        "_username": username,
        "_password": password,
        "_csrf_token": csrf_token,
    }

    # Connexion
    response = session.post(login_url, data=login_data)

    if response.status_code == 200:
        logger.debug(f"✓ Connexion réussie")
        logger.debug(f"✓ PHPSESSID: {session.cookies.get('PHPSESSID')}")
        return session
    else:
        logger.debug(f"✗ Échec de la connexion: {response.status_code}")
        return None


def get_planning_events(
    session, date_from=None, date_to=None, view="timelineWeek", base_url=None
):
def get_planning_events(
    session, date_from=None, date_to=None, view="timelineWeek", base_url=None
):
    """
    Récupère les événements du planning avec les headers complets
    """
    if base_url is None:
        base_url = "https://user.fiteco.rhsuite.silae.fr"
    url = f"{base_url}/planning/json/employee/events"
    # Dates par défaut : semaine courante
    if not date_from:
        today = datetime.now()
        date_from = today.strftime("%Y-%m-%d")

    if not date_to:
        # Semaine actuelle (7 jours)
        date_to = (datetime.now() + timedelta(days=6)).strftime("%Y-%m-%d")

    params = {
        "from": date_from,
        "to": date_to,
        "view": view,  # timelineWeek, timelineDay, month, etc.
    }

    # Headers complets basés sur la capture réseau
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8,vi;q=0.7,fr-FR;q=0.6",
        "X-Requested-With": "XMLHttpRequest",  # Important pour les requêtes AJAX
        "Referer": f"{base_url}/planning/mon-planning",
        "Referer": f"{base_url}/planning/mon-planning",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
        "sec-ch-ua": '"Microsoft Edge";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    # Ajouter les headers à la session
    session.headers.update(headers)

    logger.debug(f"\n=== Récupération des événements ===")
    logger.debug(f"Période: {date_from} à {date_to}")
    logger.debug(f"Vue: {view}")

    # Faire la requête
    response = session.get(url, params=params)

    logger.debug(f"Status: {response.status_code}")
    logger.debug(f"URL complète: {response.url}")

    if response.status_code == 200:
        try:
            data = response.json()
            logger.debug(
                f"✓ Données récupérées: {len(data) if isinstance(data, list) else 'objet JSON'}"
            )
            return data
        except json.JSONDecodeError:
            logger.debug("✗ Erreur de décodage JSON")
            logger.debug(f"Contenu: {response.text[:500]}")
            return None
    else:
        logger.debug(f"✗ Erreur: {response.status_code}")
        logger.debug(f"Réponse: {response.text[:500]}")
        return None


def parse_silae_time(time_str):
    """Parse Silae time format like '2025-10-22 10:30 CEST+0200'"""

    # Remove timezone name (CEST, CET, etc.) and keep only offset
    # '2025-10-22 10:30 CEST+0200' -> '2025-10-22 10:30 +0200'
    cleaned = re.sub(r"\s+[A-Z]{3,4}([\+\-]\d{4})", r" \1", time_str)
    return parser.parse(cleaned)


# ============================================================================


def get_planning_resources(session, base_url=None):

def get_planning_resources(session, base_url=None):
    """
    Récupère les ressources de planning
    """
    if base_url is None:
        base_url = "https://user.fiteco.rhsuite.silae.fr"
    url = f"{base_url}/planning/json/resources"

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{base_url}/planning/mon-planning",
        "Referer": f"{base_url}/planning/mon-planning",
    }

    session.headers.update(headers)
    response = session.get(url)

    if response.status_code == 200:
        try:
            return response.json()
        except json.JSONDecodeError:
            logger.debug("Erreur de décodage JSON pour les ressources")
            return None
    else:
        logger.debug(f"Erreur ressources: {response.status_code}")
        return None


def get_employee_map(resources):
    """Crée un mapping ID -> Nom d'employé"""
    employee_map = {}
    for res in resources:
        if res.get("type") == "employee":
            employee_map[res.get("id")] = {
                "name": res.get("text"),
                "firstname": res.get("firstname"),
                "lastname": res.get("lastname"),
                "function": res.get("function", {}).get("label"),
                "weekHours": res.get("weekHours"),
                "contractStart": res.get("contractStart"),
            }
    return employee_map


def find_employee_by_name(resources, search_name):
    """Trouve un employé par nom (recherche partielle, insensible à la casse)"""
    employee_map = get_employee_map(resources)
    search_name_lower = search_name.lower()

    matches = []
    for emp_id, emp_info in employee_map.items():
        if search_name_lower in emp_info["name"].lower():
            matches.append({"id": emp_id, **emp_info})

    return matches


def get_employee_shifts(events, employee_id, week_start=None, week_end=None):
    """Récupère les shifts d'un employé pour une période donnée"""
    # Convertir l'ID en string pour la comparaison
    employee_id_str = str(employee_id)

    # Filtrer les événements de cet employé
    employee_events = [e for e in events if str(e.get("employee")) == employee_id_str]

    # Filtrer par date si spécifié
    if week_start and week_end:
        filtered_events = []
        for event in employee_events:
            event_date_str = event.get("start", "").split()[0]
            if week_start <= event_date_str <= week_end:
                filtered_events.append(event)
        employee_events = filtered_events

    return employee_events


def format_shift(event):
    """Formate un événement en une représentation lisible"""
    event_type = event.get("type")
    label = event.get("label")
    code = event.get("code")
    start = event.get("start")
    end = event.get("end")
    duration = event.get("durationText")
    break_time = event.get("breakTime")

    # Parser la date et les heures
    if start:
        date_str = start.split()[0]
        time_start = start.split()[1]
    else:
        date_str = "N/A"
        time_start = "N/A"

    if end:
        time_end = end.split()[1]
    else:
        time_end = "N/A"

    result = {
        "date": date_str,
        "type": event_type,
        "label": label,
        "code": code,
        "start_time": time_start,
        "end_time": time_end,
        "duration": duration,
        "break_minutes": break_time,
    }

    if break_time and break_time > 0:
        break_start = event.get("breakTimeStart", "")
        break_end = event.get("breakTimeEnd", "")
        if break_start:
            break_start = break_start.split()[1]
        if break_end:
            break_end = break_end.split()[1]
        result["break_start"] = break_start
        result["break_end"] = break_end

    return result


def calculate_total_hours(events):
    """Calcule le total d'heures travaillées"""
    work_events = [e for e in events if e.get("type") == "WORK"]

    total_minutes = 0
    for event in work_events:
        duration_text = event.get("durationText", "0h")
        if "h" in duration_text:
            parts = duration_text.replace("h", ":").split(":")
            hours = int(parts[0]) if parts[0] else 0
            minutes = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            total_minutes += hours * 60 + minutes

    total_hours = total_minutes // 60
    total_mins = total_minutes % 60

    return f"{total_hours}h{total_mins:02d}"


def display_employee_schedule(employee_id, events, resources):
    """Affiche le planning d'un employé"""
    employee_map = get_employee_map(resources)
    employee = employee_map.get(employee_id, {})
    if not employee:
        logger.info(f"❌ Aucun employé trouvé avec id '{employee_id}'")
        return

    logger.info(f"\n{'='*60}")
    logger.info(f"📋 PLANNING DE {employee['name'].upper()}")
    logger.info(f"{'='*60}")
    logger.info(f"Fonction: {employee['function']}")
    logger.info(f"Heures hebdomadaires: {employee['weekHours']}")
    logger.info(f"Date d'embauche: {employee['contractStart']}")
    logger.info()

    # Récupérer les shifts
    shifts = get_employee_shifts(events, employee_id)

    if not shifts:
        logger.info("❌ Aucun shift trouvé pour cette période")
        return

    logger.info(f"\n{'='*60}")
    logger.info(f"📅 SHIFTS DE LA SEMAINE")
    logger.info(f"{'='*60}\n")

    # Afficher chaque shift
    for event in shifts:
        shift = format_shift(event)

        if shift["type"] == "WORK":
            logger.info(f"📅 {shift['date']} - {shift['label']} ({shift['code']})")
            logger.info(f"   ⏰ {shift['start_time']} → {shift['end_time']}")
            logger.info(f"   ⌛ Durée: {shift['duration']}")
            if shift.get("break_minutes", 0) > 0:
                logger.info(
                    f"   ☕ Pause: {shift['break_minutes']} min ({shift.get('break_start')} - {shift.get('break_end')})"
                )
        else:
            logger.info(f"📅 {shift['date']} - {shift['label']} ({shift['code']})")
            logger.info(f"   🏠 Repos/Absence")
        logger.info()

    # Résumé
    work_shifts = [e for e in shifts if e.get("type") == "WORK"]
    absence_shifts = [e for e in shifts if e.get("type") == "ABSENCE"]

    logger.info(f"\n{'='*60}")
    logger.info(f"📊 RÉSUMÉ")
    logger.info(f"{'='*60}")
    logger.info(f"Jours travaillés: {len(work_shifts)}")
    logger.info(f"Jours d'absence/repos: {len(absence_shifts)}")
    logger.info(f"Total heures: {calculate_total_hours(shifts)}")
    logger.info()
