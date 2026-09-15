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
STATE_FILE = Path("state.json")
DEBUG_TXT = Path("debug_page.txt")
DEBUG_HTML = Path("debug_page.html")
JOBS_JSON = Path("docs/jobs.json")  # lu par la page web (dossier docs/ = GitHub Pages)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # ex: "julien-nato-taleo-xk92"

# Mots-clés basés sur le profil de Julien (finance/budget NCIA) — à ajuster
# librement selon ce qui matche bien ou pas en pratique.
MATCH_KEYWORDS = [
    "financial", "finance", "budget", "ipsas", "procurement", "contracting",
    "accounting", "audit", "resource management", "cost estimation",
    "business management and control", "cost analysis",
]


def score_match(title: str):
    title_lower = title.lower()
    matched = [kw for kw in MATCH_KEYWORDS if kw in title_lower]
    return len(matched), matched


def fetch_jobs():
    """Retourne (total_count_devine, {job_number: job_title})."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # Tente de passer à 100 résultats par page si le sélecteur existe,
        # pour limiter le nombre de pages à parcourir. Ignoré si absent.
        try:
            page.select_option("select[name*='PageSize'], select[id*='PageSize']", "100")
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        page.wait_for_timeout(3000)  # laisse le temps au rendu JS de finir

        jobs = {}
        page_num = 1
        while True:
            text = page.inner_text("body")
            html = page.content()

            # Dump debug (écrasé à chaque page, on garde surtout la dernière
            # pour l'instant — utile pour calibrer les sélecteurs ensemble).
            DEBUG_TXT.write_text(text, encoding="utf-8")
            DEBUG_HTML.write_text(html, encoding="utf-8")

            # Chaque offre affiche "Job Number: 123456" quelque part dans son bloc.
            # On découpe le texte brut autour de ce marqueur et on essaie de
            # récupérer le titre = dernière ligne non vide juste avant le marqueur
            # dans le morceau précédent (approche approximative pour la V1,
            # à confirmer/ajuster avec debug_page.txt).
            chunks = re.split(r"Job Number:\s*", text)
            for i, chunk in enumerate(chunks[1:], start=1):
                m = re.match(r"(\d+)", chunk.strip())
                if not m:
                    continue
                job_number = m.group(1)
                prev_lines = [l.strip() for l in chunks[i - 1].splitlines() if l.strip()]
                title = prev_lines[-1] if prev_lines else f"Offre {job_number}"
                if job_number not in jobs:
                    jobs[job_number] = title

            # Pagination : cherche un lien/bouton "page suivante" actif.
            next_btn = page.query_selector("a[title*='Next'], a[aria-label*='Next']")
            if next_btn and next_btn.is_enabled():
                next_btn.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                page_num += 1
                if page_num > 30:  # garde-fou anti boucle infinie
                    break
            else:
                break

        browser.close()
        return len(jobs), jobs


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


def build_jobs_export(current_jobs, new_ids):
    """Construit la liste enrichie (titre, nouveauté, score de correspondance)
    consommée par la page web, triée : nouvelles + correspondances en premier."""
    entries = []
    for job_number, title in current_jobs.items():
        score, matched_keywords = score_match(title)
        entries.append({
            "job_number": job_number,
            "title": title,
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

    entries = build_jobs_export(current_jobs, new_ids)
    JOBS_JSON.parent.mkdir(parents=True, exist_ok=True)
    JOBS_JSON.write_text(
        json.dumps({"total": total, "jobs": entries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    new_matches = [e for e in entries if e["is_new"] and e["match_score"] > 0]

    if first_run:
        send_notification(
            "NATO Taleo — Suivi activé",
            f"{total} offre(s) en ligne actuellement. Les prochaines vérifications signaleront les nouveautés.",
        )
    elif new_ids:
        titles = "\n".join(
            f"• {'⭐ ' if e['is_new'] and e['match_score'] > 0 else ''}{e['title']} (#{e['job_number']})"
            for e in entries if e["is_new"]
        )
        header = f"NATO Taleo — {len(new_ids)} nouvelle(s) offre(s)"
        if new_matches:
            header += f" dont {len(new_matches)} correspondance(s) ⭐"
        send_notification(header, f"Total en ligne : {total}\n\n{titles}")
    else:
        print(f"Aucune nouvelle offre. Total actuel : {total}")

    save_state(current_jobs)
    print(f"OK — {total} offres détectées, {len(new_ids)} nouvelle(s), {len(new_matches)} correspondance(s).")


if __name__ == "__main__":
    sys.exit(main())