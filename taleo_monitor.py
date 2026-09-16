"""
Surveillance des offres d'emploi NATO Taleo.

- Charge la page de recherche avec un navigateur headless (le site est en JS,
  un simple requests.get() ne suffit pas).
- Extrait le nombre total d'offres et la liste des offres (numéro + titre).
- Compare avec le dernier état connu (state.json) pour détecter les nouvelles.
- Envoie une notification push iPhone via ntfy.sh.
- Sauvegarde toujours un dump brut de la page (debug_page.txt / debug_page.html)
  pour qu'on puisse calibrer les sélecteurs si l'extraction est incomplète.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://nato.taleo.net/careersection/2/jobsearch.ftl?lang=en"
DETAIL_URL = "https://nato.taleo.net/careersection/2/jobdetail.ftl?job={job_number}&lang=en"
STATE_FILE = Path("state.json")
DEBUG_TXT = Path("debug_page.txt")
DEBUG_HTML = Path("debug_page.html")
DEBUG_DETAIL_TXT = Path("debug_detail_page.txt")  # dernière fiche offre visitée, pour calibrer
JOBS_JSON = Path("docs/jobs.json")  # lu par la page web (dossier docs/ = GitHub Pages)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # ex: "julien-nato-taleo-xk92"

# Mots-clés basés sur le profil de Julien (finance/budget NCIA) et les postes
# qu'il vise concrètement (Staff Assistant/Officer Budget, Finance & Travel) —
# à ajuster librement selon ce qui matche bien ou pas en pratique.
MATCH_KEYWORDS = [
    "financial", "finance", "budget", "ipsas", "procurement", "contracting",
    "accounting", "audit", "resource management", "cost estimation",
    "business management and control", "cost analysis", "travel", "treasury", "payroll",
]


def score_match(title: str):
    title_lower = title.lower()
    matched = [kw for kw in MATCH_KEYWORDS if kw in title_lower]
    return len(matched), matched


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
            except Exception:
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

            # Pagination : le site affiche un lien texte "Next" (pas d'attribut
            # title/aria-label dessus) — on le cherche par son texte exact.
            # Le site affiche "Jobs - Page X out of Y" — bien plus fiable que
            # l'état (peu fiable) du lien "Next" pour savoir si on est à la
            # dernière page.
            m_page = re.search(r"Page\s*(\d+)\s*out of\s*(\d+)", text, re.I)
            on_last_page = bool(m_page) and int(m_page.group(1)) >= int(m_page.group(2))

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


def load_previous_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(jobs):
    STATE_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def send_notification(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC non défini — notification non envoyée.")
        print(f"[{title}] {message}")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": "default"},
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
    previous_jobs = load_previous_state()

    new_ids = set(current_jobs) - set(previous_jobs)
    first_run = len(previous_jobs) == 0

    # Le salaire n'est visible que sur la fiche détaillée de chaque offre (pas
    # sur la page de recherche) — on ne la visite que pour les offres qui
    # matchent le profil, pour ne pas ralentir le run sur les 67 offres.
    matching_ids = [jn for jn, info in current_jobs.items() if score_match(info["title"])[0] > 0]
    salaries = fetch_salaries(matching_ids)

    entries = build_jobs_export(current_jobs, new_ids, salaries)
    JOBS_JSON.parent.mkdir(parents=True, exist_ok=True)
    JOBS_JSON.write_text(
        json.dumps({"total": total, "jobs": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    new_matches = [e for e in entries if e["is_new"] and e["match_score"] > 0]
    current_matches = [e for e in entries if e["match_score"] > 0]

    if first_run:
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
        send_notification(header, f"Total en ligne : {total}\n\n{titles}")
    elif current_matches:
        titles = "\n".join(
            f"⭐ {e['title']} — {e['location']} (#{e['job_number']})"
            for e in current_matches
        )
        send_notification(
            f"NATO Taleo — Pas de nouvelle offre, {len(current_matches)} correspondance(s) active(s)",
            f"Total en ligne : {total}\n\n{titles}",
        )
    else:
        send_notification(
            "NATO Taleo — Pas de nouvelle offre",
            f"Total en ligne : {total}. Aucune correspondance active pour le moment.",
        )

    save_state(current_jobs)
    print(f"OK — {total} offres détectées, {len(new_ids)} nouvelle(s), {len(new_matches)} correspondance(s).")


if __name__ == "__main__":
    sys.exit(main())
