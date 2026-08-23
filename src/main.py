"""
Veille scientifique automatisée.

Étapes : OpenAlex + arXiv -> normalisation -> dédup (vs seen_ids.json)
-> filtre mots-clés -> scoring LLM (Gemini) -> digest -> email.

Toutes les variables sensibles (clé API, identifiants email) sont lues
depuis les variables d'environnement, fournies par le workflow GitHub
Actions à partir des secrets du repo.
"""

import datetime as dt
import json
import os
import re
import smtplib
import time
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText

import requests
import yaml

SEEN_FILE = "seen_ids.json"
OPENALEX_URL = "https://api.openalex.org/works"
OPENALEX_SOURCES_URL = "https://api.openalex.org/sources"
S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
ARXIV_URL = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


# ───────────────────────── Config & état ─────────────────────────

def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen_ids():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(ids):
    # on garde une taille raisonnable pour que le fichier ne grossisse pas indéfiniment
    trimmed = list(ids)[-5000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


# ───────────────────────── Sources ─────────────────────────

def reconstruct_abstract(inverted_index):
    """OpenAlex renvoie les résumés sous forme d'index inversé (pour des raisons de droits)."""
    if not inverted_index:
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def fetch_abstract_by_title(title):
    try:
        r = requests.get(S2_URL, params={"query": title, "fields": "abstract", "limit": 1}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        return (data[0].get("abstract") or "") if data else ""
    except Exception:
        return ""


def resolve_source_id(name, api_key):
    """Trouve l'ID OpenAlex d'une source (ex: medRxiv) à partir de son nom."""
    try:
        r = requests.get(
            OPENALEX_SOURCES_URL,
            params={"search": name, "per-page": 1, "api_key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0]["id"] if results else None
    except Exception as e:
        print(f"[warn] Résolution de la source '{name}' échouée: {e}")
        return None


def fetch_openalex(keyword, year, per_page, contact_email, api_key, source_id=None):
    filters = [f"publication_year:{year}"]
    if source_id:
        filters.append(f"locations.source.id:{source_id}")
    params = {
        "search": keyword,
        "filter": ",".join(filters),
        "per-page": per_page,
        "sort": "publication_date:desc",
        "api_key": api_key,
    }
    if contact_email:
        params["mailto"] = contact_email
    try:
        r = requests.get(OPENALEX_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"[warn] OpenAlex a échoué pour '{keyword}': {e}")
        return []

    records = []
    for w in data.get("results", []):
        abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
        title = (w.get("title") or "").strip()
        if not abstract:
            abstract = fetch_abstract_by_title(title)
        if not abstract:
            continue  # toujours pas de résumé : on saute
        records.append({
            "id": w.get("id", ""),
            "source": "medrxiv" if source_id else "openalex",
            "title": title,
            "abstract": abstract,
            "authors": ", ".join(
                a["author"]["display_name"] for a in w.get("authorships", [])[:5]
            ),
            "date": w.get("publication_date", "") or "",
            "url": w.get("id", ""),
            "doi": w.get("doi", "") or "",
        })
    return records


def fetch_arxiv(keyword, categories, max_results):
    cat_query = " OR ".join(f"cat:{c}" for c in categories) if categories else ""
    kw_query = f'all:"{keyword}"'
    search_query = f"({kw_query}) AND ({cat_query})" if cat_query else kw_query
    params = {
        "search_query": search_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    }
    try:
        r = requests.get(ARXIV_URL, params=params, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"[warn] arXiv a échoué pour '{keyword}': {e}")
        return []

    records = []
    for entry in root.findall("atom:entry", ATOM_NS):
        arxiv_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS).strip()
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS).strip().replace("\n", " ")
        abstract = entry.findtext("atom:summary", default="", namespaces=ATOM_NS).strip().replace("\n", " ")
        authors = ", ".join(
            a.findtext("atom:name", default="", namespaces=ATOM_NS)
            for a in entry.findall("atom:author", ATOM_NS)[:5]
        )
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)[:10]
        if not (arxiv_id and title and abstract):
            continue
        records.append({
            "id": arxiv_id,
            "source": "arxiv",
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "date": published,
            "url": arxiv_id,
            "doi": "",
        })
    return records


# ───────────────────────── Normalisation / dédup / filtre ─────────────────────────

def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def dedupe(records, seen_ids):
    seen_titles = set()
    unique = []
    for r in records:
        if r["id"] in seen_ids:
            continue
        nt = normalize_title(r["title"])
        if not nt or nt in seen_titles:
            continue
        seen_titles.add(nt)
        unique.append(r)
    return unique


def keyword_filter(records, keywords):
    kws = [k.lower() for k in keywords]
    out = []
    for r in records:
        text = (r["title"] + " " + r["abstract"]).lower()
        if any(k in text for k in kws):
            out.append(r)
    return out


# ───────────────────────── Scoring LLM (Gemini) ─────────────────────────

def score_batch(records, interests, api_key, model, max_retries=3):
    if not records:
        return []

    papers_block = "\n\n".join(
        f'[{i}] ID: {r["id"]}\nTitre: {r["title"]}\nRésumé: {r["abstract"][:1500]}'
        for i, r in enumerate(records)
    )
    prompt = f"""Tu es un assistant de veille scientifique. Voici le profil de recherche de l'utilisateur :
"{interests}"

Voici une liste d'articles récents. Pour CHAQUE article, évalue sa pertinence
par rapport à ce profil, sur la seule base du titre et du résumé fournis.

{papers_block}

Réponds UNIQUEMENT avec un tableau JSON (aucun texte autour), un objet par
article, dans le même ordre, avec exactement ce format :
[{{"index": 0, "score": 7, "summary": "résumé en 2-3 phrases en français", "why": "une phrase expliquant pourquoi c'est pertinent (ou pas) pour ce profil"}}]
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    url = GEMINI_URL_TMPL.format(model=model)
    for attempt in range(max_retries):
        try:
            r = requests.post(url, params={"key": api_key}, json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            results = json.loads(text)
            break
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (503, 429) and attempt < max_retries - 1:
                wait = 10 * (attempt + 1)
                print(f"[warn] {e.response.status_code}, nouvelle tentative dans {wait}s...")
                time.sleep(wait)
                continue
            print(f"[warn] Scoring Gemini échoué pour un lot de {len(records)} papiers: {e}")
            try:
                lst = requests.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key}, timeout=15,
                ).json()
                names = [m["name"] for m in lst.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                print(f"[debug] Modèles disponibles pour cette clé: {names}")
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"[warn] Scoring Gemini échoué pour un lot de {len(records)} papiers: {e}")
            raise

    scored = []
    for item in results:
        idx = item.get("index")
        if idx is None or not (0 <= idx < len(records)):
            continue
        rec = records[idx]
        rec["score"] = item.get("score", 0)
        rec["summary"] = item.get("summary", "")
        rec["why"] = item.get("why", "")
        scored.append(rec)
    return scored


def score_all(records, interests, api_key, model, batch_size=8, pause_seconds=15):
    scored = []
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        scored.extend(score_batch(batch, interests, api_key, model))
        if i + batch_size < len(records):
            time.sleep(pause_seconds)  # marge pour rester sous les limites du tier gratuit
    return scored


# ───────────────────────── Digest & envoi ─────────────────────────

# ───────────────────────── Site GitHub Pages ─────────────────────────

DOCS_DIR = "docs"
WEEKS_DIR = os.path.join(DOCS_DIR, "weeks")
WEEKS_META_FILE = os.path.join(DOCS_DIR, "weeks.json")

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{css_path}">
</head>
<body>
<div class="page">
{content}
</div>
</body>
</html>"""


def html_escape(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_weeks_meta():
    if os.path.exists(WEEKS_META_FILE):
        with open(WEEKS_META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_weeks_meta(meta):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(WEEKS_META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def build_week_page(candidates, selected_ids, period_label):
    rows = ""
    for r in sorted(candidates, key=lambda r: r["date"], reverse=True):
        selected = r["id"] in selected_ids
        link = r.get("doi") or r["url"]
        badge = '<span class="badge">Sélectionné · envoyé par email</span>' if selected else ""
        rows += f"""
        <article class="paper">
          <div class="paper-bar {'bar-selected' if selected else ''}"></div>
          <div class="paper-body">
            <h3 class="title{' selected' if selected else ''}"><a href="{html_escape(link)}" target="_blank" rel="noopener">{html_escape(r['title'])}</a></h3>
            {badge}
            <p class="meta">{html_escape(r['authors'])} — {html_escape(r['date'])} — {html_escape(r['source'])}</p>
            <p class="doi">DOI : {html_escape(r.get('doi') or '—')}</p>
            <p class="abstract">{html_escape(r['abstract'])}</p>
          </div>
        </article>"""
    content = f"""
      <a class="back" href="../index.html">← Retour à l'accueil</a>
      <h1>{html_escape(period_label)}</h1>
      <p class="count">{len(candidates)} article(s) trouvé(s) après filtres, {len(selected_ids)} retenu(s) et envoyé(s) par email.</p>
      <div class="papers">{rows}</div>"""
    return PAGE_TEMPLATE.format(title=f"Veille — {period_label}", css_path="../assets/style.css", content=content)


def build_index_html(weeks_meta):
    weeks_sorted = sorted(weeks_meta, key=lambda w: w["date"], reverse=True)
    years = sorted({w["year"] for w in weeks_sorted}, reverse=True)
    sections = ""
    for y in years:
        items = "".join(
            f"""<li><a class="week-link" href="weeks/{w['filename']}">
                  <span class="week-range">{html_escape(w['period_label'])}</span>
                  <span class="week-count">{w['selected']}/{w['total']} retenus</span>
                </a></li>"""
            for w in weeks_sorted if w["year"] == y
        )
        sections += f"""<details class="year-block" {"open" if y == years[0] else ""}>
          <summary>{y}</summary>
          <ul class="week-list">{items}</ul>
        </details>"""
    body = sections or '<p class="empty">Aucune veille pour le moment — revenez après le premier run.</p>'
    content = f"""
      <h1>Veille scientifique</h1>
      <p class="subtitle">Archive hebdomadaire des articles suivis automatiquement.</p>
      <div class="years">{body}</div>"""
    return PAGE_TEMPLATE.format(title="Veille scientifique", css_path="assets/style.css", content=content)


def update_site(candidates, selected_ids, period_label, run_date):
    os.makedirs(WEEKS_DIR, exist_ok=True)
    week_filename = f"{run_date}.html"
    with open(os.path.join(WEEKS_DIR, week_filename), "w", encoding="utf-8") as f:
        f.write(build_week_page(candidates, selected_ids, period_label))

    weeks_meta = [w for w in load_weeks_meta() if w["filename"] != week_filename]
    weeks_meta.append({
        "date": run_date,
        "year": int(run_date[:4]),
        "period_label": period_label,
        "filename": week_filename,
        "total": len(candidates),
        "selected": len(selected_ids),
    })
    save_weeks_meta(weeks_meta)
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index_html(weeks_meta))


def build_digest_html(papers, period_label):
    if not papers:
        return f"<p>Aucun nouvel article suffisamment pertinent trouvé ({period_label}).</p>"

    items = ""
    for p in papers:
        items += f"""
        <div style="margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #ddd;">
          <h3 style="margin:0 0 4px;"><a href="{p['url']}">{p['title']}</a></h3>
          <p style="margin:0 0 4px;color:#555;font-size:13px;">
            {p['authors']} — {p['date']} — source : {p['source']} — score {p['score']}/10
          </p>
          <p style="margin:0 0 4px;">{p['summary']}</p>
          <p style="margin:0;font-style:italic;color:#333;">Pourquoi : {p['why']}</p>
        </div>
        """
    return f"<h2>Veille scientifique — {period_label}</h2>{items}"


def send_email(subject, html_body, user, password, to_addr):
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


# ───────────────────────── Orchestration ─────────────────────────

def main():
    config = load_config()
    since_date = (dt.date.today() - dt.timedelta(days=config["days_back"])).isoformat()
    year = dt.date.today().year
    api_key = os.environ["OPENALEX_API_KEY"]
    seen_ids = load_seen_ids()

    medrxiv_id = resolve_source_id("medRxiv", api_key) if config.get("include_medrxiv", True) else None
    openalex_max = config.get("openalex_max_results", config["max_results_per_query"])

    all_records = []
    for kw in config["keywords"]:
        all_records += fetch_openalex(kw, year, openalex_max, config.get("contact_email"), api_key)
        if medrxiv_id:
            all_records += fetch_openalex(kw, year, openalex_max, config.get("contact_email"), api_key, source_id=medrxiv_id)
        if config.get("arxiv_categories"):
            all_records += fetch_arxiv(kw, config["arxiv_categories"], config["max_results_per_query"])

    # OpenAlex/medRxiv n'ont souvent qu'une date précise à l'année près : on ne
    # filtre par date que pour arXiv, qui la fournit toujours. Pour les autres,
    # c'est la dédup (seen_ids.json) qui garantit qu'on ne revoit pas deux fois
    # le même article d'une semaine à l'autre.
    all_records = [r for r in all_records if r["source"] != "arxiv" or r["date"] >= since_date]

    candidates = dedupe(all_records, seen_ids)
    candidates = keyword_filter(candidates, config["keywords"])
    print(f"{len(all_records)} résultats bruts -> {len(candidates)} candidats après dédup/filtre")

    scored = score_all(
        candidates,
        config["interests"],
        os.environ["GEMINI_API_KEY"],
        config.get("model", "gemini-2.5-flash"),
    )

    # tous les candidats examinés sont marqués comme vus, retenus ou non,
    # pour ne jamais les re-analyser (et gaspiller des appels LLM) plus tard
    seen_ids.update(r["id"] for r in candidates)
    save_seen_ids(seen_ids)

    top = sorted(
        [r for r in scored if r["score"] >= config["min_score"]],
        key=lambda r: -r["score"],
    )[: config["top_n"]]

    period_label = f"{since_date} → {dt.date.today().isoformat()}"
    selected_ids = {r["id"] for r in top}
    update_site(candidates, selected_ids, period_label, dt.date.today().isoformat())

    html = build_digest_html(top, period_label)
    # Add link to website
    html += """<div> <a href="https://quentinlaval.github.io/Paper-Agentic-Research/"> website </a> </div>"""

    send_always = os.environ.get("SEND_EMPTY_DIGEST", "false").lower() == "true"
    if top or send_always:
        send_email(
            subject=f"📚 Veille scientifique — {len(top)} article(s) pertinent(s)",
            html_body=html,
            user=os.environ["EMAIL_USER"],
            password=os.environ["EMAIL_PASS"],
            to_addr=os.environ["EMAIL_TO"],
        )
        print(f"Email envoyé avec {len(top)} article(s).")
    else:
        print("Rien de suffisamment pertinent cette période, pas d'email envoyé.")


if __name__ == "__main__":
    main()
