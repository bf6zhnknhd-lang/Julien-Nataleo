"""
Surveillance des offres d'emploi NATO Taleo.

- Charge la page de recherche avec un navigateur headless (le site est en JS,
  un simple requests.get() ne suffit pas).
- Extrait le nombre total d'offres et la liste des offres (numéro + titre).
- Compare avec le dernier état connu (state.json) pour détecter les nouvelles
  ET les offres retirées (historique).
- Journalise un point de statistique à chaque run (total / correspondances).
- Envoie une notification push iPhone via ntfy.sh, avec priorité relevée si
  une échéance approche.
- Sauvegarde toujours un dump brut de la page (debug_page.txt / debug_page.html)
  pour qu'on puisse calibrer les sélecteurs si l'extraction est incomplète.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://nato.taleo.net/careersection/2/jobsearch.ftl?lang=en"
DETAIL_URL = "https://nato.taleo.net/careersection/2/jobdetail.ftl?job={job_number}&lang=en"
STATE_FILE = Path("state.json")
DEBUG_TXT = Path("debug_page.txt")
DEBUG_HTML = Path("debug_page.html")
DEBUG_DETAIL_TXT = Path("debug_detail_page.txt")  # dernière fiche offre visitée, pour calibrer
JOBS_JSON = Path("docs/jobs.json")          # lu par la page web (dossier docs/ = GitHub Pages)
HISTORY_JSON = Path("docs/history.json")    # offres retirées/pourvues au fil du temps
STATS_JSON = Path("docs/history_stats.json")  # un point par run : total + correspondances

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # ex: "julien-nato-taleo-xk92"
WEEKLY_SUMMARY = os.environ.get("WEEKLY_SUMMARY", "").lower() in ("1", "true", "yes")

MAX_HISTORY_ENTRIES = 300
MAX_STATS_POINTS = 200
URGENT_DEADLINE_DAYS = 3  # priorité ntfy relevée si une échéance tombe sous ce seuil

# Mots-clés pondérés (poids plus élevé = plus déterminant pour le score de
# correspondance) — à ajuster librement selon ce qui matche bien en pratique.
MATCH_KEYWORDS = {
    "budget": 3, "ipsas": 3, "financial": 2, "finance": 2, "procurement": 2,
    "accounting": 2, "cost estimation": 2, "cost analysis": 2,
    "business management and control": 2, "contracting": 2, "audit": 1,
    "resource management": 1, "travel": 1, "treasury": 1, "payroll": 1,
}


def score_match(title: str):
    title_lower = title.lower()
    matched = [kw for kw in MATCH_KEYWORDS if kw in title_lower]
    score = sum(MATCH_KEYWORDS[kw] for kw in matched)
    return score, matched


def parse_deadline(deadline_str):
    """Parse une échéance du type '31-Dec-2026, 10:59:00 PM' -> datetime, ou
    None si le format ne correspond pas (on ne bloque jamais dessus)."""
    if not deadline_str:
        return None
    try:
        return datetime.strptime(deadline_str.strip(), "%d-%b-%Y, %I:%M:%S %p").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_salaries(job_numbers):
    """Visite la fiche détaillée de chaque offre listée (uniquement les
    correspondances, pour ne pas alourdir le run) et tente d'en extraire le
    salaire. Retourne {job_number: salaire_ou_chaine_vide}."""
    salaries = {}
    if not job_numbers:
        return salaries
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for job_number in job_numbers:
            try:
                page.goto(DETAIL_URL.format(job_number=job_number), wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(2000)
                text = page.inner_text("body")
                DEBUG_DETAIL_TXT.write_text(text, encoding="utf-8")  # dernière fiche visitée
                m = re.search(r"Salary\s*\(Pay Basis\)\s*:?\s*([^\n]{3,80})", text, re.I)
                salaries[job_number] = m.group(1).strip() if m else ""
            except Exception as e:
                print(f"Erreur en visitant la fiche de l'offre #{job_number} : {e!r}")
                salaries[job_number] = ""
        browser.close()
    return salaries


def fetch_jobs():
    """Retourne (total_annonce, {job_number: infos_offre})."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # laisse le temps au rendu JS de finir

        jobs = {}
        announced_total = None
        page_num = 1
        while True:
            text = page.inner_text("body")
            html = page.content()

            # Dump debug (écrasé à chaque page — reflète donc la dernière page
            # visitée, utile pour vérifier que la pagination avance bien).
            DEBUG_TXT.write_text(text, encoding="utf-8")
            DEBUG_HTML.write_text(html, encoding="utf-8")

            # Le site affiche directement le total réel : "Search Results (67 jobs found)".
            if announced_total is None:
                m_total = re.search(r"Search Results\s*\((\d+)\s*jobs? found\)", text, re.I)
                if m_total:
                    announced_total = int(m_total.group(1))

            # Chaque offre affiche : "Job Number: 123456 - Lieu", puis
            # "Application Deadline: ...", puis "NATO Body: Org - Grade: G..".
            # Le titre est la dernière ligne non vide juste avant, dans le
            # morceau de texte précédent.
            chunks = re.split(r"Job Number:\s*", text)
            for i, chunk in enumerate(chunks[1:], start=1):
                stripped = chunk.strip()
                m = re.match(
                    r"(?P<jobnum>\d+)\s*-\s*(?P<location>[^\n]+)\s*\n+"
                    r"Application Deadline:\s*(?P<deadline>[^\n]+)\s*\n+"
                    r"NATO Body:\s*(?P<org>.+?)\s*-\s*Grade:\s*(?P<grade>[^\n]*)",
                    stripped,
                    re.S,
                )
                if not m:
                    m_num = re.match(r"(\d+)", stripped)
                    if not m_num:
                        continue
                    job_number = m_num.group(1)
                    location = deadline = org = grade = ""
                else:
                    job_number = m.group("jobnum")
                    location = m.group("location").strip()
                    deadline = m.group("deadline").strip()
                    org = m.group("org").strip()
                    grade = m.group("grade").strip()

                prev_lines = [l.strip() for l in chunks[i - 1].splitlines() if l.strip()]
                title = prev_lines[-1] if prev_lines else f"Offre {job_number}"
                if job_number not in jobs:
                    jobs[job_number] = {
                        "title": title,
                        "location": location,
                        "deadline": deadline,
                        "org": org,
                        "grade": grade,
                    }

            # Le site affiche "Jobs - Page X out of Y" — bien plus fiable que
            # l'état (peu fiable) du lien "Next" pour savoir si on est à la
            # dernière page.
            m_page = re.search(r"Page\s*(\d+)\s*out of\s*(\d+)", text, re.I)
            on_last_page = bool(m_page) and int(m_page.group(1)) >= int(m_page.group(2))

            # Le site affiche un lien texte "Next" (pas d'attribut
            # title/aria-label dessus) — on le cherche par son texte exact.
            next_links = page.locator("a").filter(has_text=re.compile(r"^\s*Next\s*$", re.I))
            if not on_last_page and next_links.count() > 0:
                # Le site marque ce lien aria-disabled="true" même quand il est
                # réellement cliquable (le vrai état est géré en JS, pas via
                # l'attribut disabled standard) — Playwright refuse donc de le
                # cliquer normalement. On déclenche le clic natif DOM à la place,
                # qui passe par le même onclick JS que ferait un vrai clic.
                page.evaluate(
                    "el => el.click()",
                    next_links.first.element_handle(),
                )
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                page_num += 1
                if page_num > 30:  # garde-fou anti boucle infinie
                    break
            else:
                break

        browser.close()
        total = announced_total if announced_total is not None else len(jobs)
        return total, jobs


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_history(previous_jobs, current_jobs):
    """Ajoute au fichier d'historique les offres qui ont disparu depuis le
    dernier run (retirées ou pourvues). Retourne la liste complète (limitée)."""
    history = load_json(HISTORY_JSON, [])
    removed_ids = set(previous_jobs) - set(current_jobs)
    now = datetime.now(timezone.utc).isoformat()
    for job_number in removed_ids:
        info = previous_jobs[job_number]
        history.append({
            "job_number": job_number,
            "title": info.get("title", ""),
            "location": info.get("location", ""),
            "removed_at": now,
        })
    history = history[-MAX_HISTORY_ENTRIES:]
    save_json(HISTORY_JSON, history)
    return history


def update_stats(total, matches_count):
    stats = load_json(STATS_JSON, [])
    stats.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "matches": matches_count,
    })
    stats = stats[-MAX_STATS_POINTS:]
    save_json(STATS_JSON, stats)


def send_notification(title, message, priority="default"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC non défini — notification non envoyée.")
        print(f"[{title}] {message}")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": priority},
        timeout=15,
    )


def build_jobs_export(current_jobs, new_ids, salaries):
    """Construit la liste enrichie (titre, lieu, grade, salaire, nouveauté,
    score de correspondance) consommée par la page web, triée : nouvelles +
    correspondances en premier."""
    entries = []
    for job_number, info in current_jobs.items():
        score, matched_keywords = score_match(info["title"])
        entries.append({
            "job_number": job_number,
            "title": info["title"],
            "location": info.get("location", ""),
            "deadline": info.get("deadline", ""),
            "org": info.get("org", ""),
            "grade": info.get("grade", ""),
            "salary": salaries.get(job_number, ""),
            "url": f"https://nato.taleo.net/careersection/2/jobdetail.ftl?job={job_number}",
            "is_new": job_number in new_ids,
            "match_score": score,
            "matched_keywords": matched_keywords,
        })
    entries.sort(key=lambda e: (not e["is_new"], -e["match_score"], e["title"]))
    return entries


def main():
    total, current_jobs = fetch_jobs()
    previous_jobs = load_previous_state = load_json(STATE_FILE, {})

    new_ids = set(current_jobs) - set(previous_jobs)
    first_run = len(previous_jobs) == 0

    history = update_history(previous_jobs, current_jobs)

    # Le salaire n'est visible que sur la fiche détaillée de chaque offre (pas
    # sur la page de recherche) — on ne la visite que pour les offres qui
    # matchent le profil, pour ne pas ralentir le run sur toutes les offres.
    matching_ids = [jn for jn, info in current_jobs.items() if score_match(info["title"])[0] > 0]
    print(f"{len(matching_ids)} offre(s) correspondante(s) ce run : {matching_ids}")
    salaries = fetch_salaries(matching_ids)

    entries = build_jobs_export(current_jobs, new_ids, salaries)
    save_json(JOBS_JSON, {"total": total, "jobs": entries, "generated_at": datetime.now(timezone.utc).isoformat()})

    new_matches = [e for e in entries if e["is_new"] and e["match_score"] > 0]
    current_matches = [e for e in entries if e["match_score"] > 0]
    update_stats(total, len(current_matches))

    # Priorité relevée si une offre nouvelle ou correspondante a une échéance
    # proche (sous URGENT_DEADLINE_DAYS jours).
    now = datetime.now(timezone.utc)
    urgent = False
    for e in entries:
        if e["is_new"] or e["match_score"] > 0:
            dl = parse_deadline(e["deadline"])
            if dl and (dl - now).days <= URGENT_DEADLINE_DAYS:
                urgent = True
                break
    priority = "high" if urgent else "default"

    if WEEKLY_SUMMARY:
        titles = "\n".join(
            f"⭐ {e['title']} — {e['location']} (#{e['job_number']})"
            for e in current_matches
        ) or "Aucune correspondance active cette semaine."
        send_notification(
            f"NATO Taleo — Résumé hebdomadaire ({total} offres, {len(current_matches)} correspondance(s))",
            titles,
            priority=priority,
        )
    elif first_run:
        send_notification(
            "NATO Taleo — Suivi activé",
            f"{total} offre(s) en ligne actuellement. Les prochaines vérifications signaleront les nouveautés.",
        )
    elif new_ids:
        titles = "\n".join(
            f"• {'⭐ ' if e['is_new'] and e['match_score'] > 0 else ''}{e['title']} — {e['location']} (#{e['job_number']})"
            for e in entries if e["is_new"]
        )
        header = f"NATO Taleo — {len(new_ids)} nouvelle(s) offre(s)"
        if new_matches:
            header += f" dont {len(new_matches)} correspondance(s) ⭐"
        if urgent:
            header += " ⚠️ échéance proche"
        send_notification(header, f"Total en ligne : {total}\n\n{titles}", priority=priority)
    elif current_matches:
        titles = "\n".join(
            f"⭐ {e['title']} — {e['location']} (#{e['job_number']})"
            for e in current_matches
        )
        header = f"NATO Taleo — Pas de nouvelle offre, {len(current_matches)} correspondance(s) active(s)"
        if urgent:
            header += " ⚠️ échéance proche"
        send_notification(header, f"Total en ligne : {total}\n\n{titles}", priority=priority)
    else:
        send_notification(
            "NATO Taleo — Pas de nouvelle offre",
            f"Total en ligne : {total}. Aucune correspondance active pour le moment.",
        )

    save_json(STATE_FILE, current_jobs)
    print(f"OK — {total} offres détectées, {len(new_ids)} nouvelle(s), {len(current_matches)} correspondance(s) actuelle(s) au total (dont {len(new_matches)} nouvelle(s)). {len(history)} offre(s) dans l'historique.")


if __name__ == "__main__":
    sys.exit(main())

