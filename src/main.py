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


def fetch_openalex(keyword, since_date, per_page, contact_email, api_key):
    params = {
        "search": keyword,
        "filter": f"from_publication_date:{since_date}",
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
            "source": "openalex",
            "title": title,
            "abstract": abstract,
            "authors": ", ".join(
                a["author"]["display_name"] for a in w.get("authorships", [])[:5]
            ),
            "date": w.get("publication_date", "") or "",
            "url": w.get("id", ""),
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

def score_batch(records, interests, api_key, model):
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
    try:
        r = requests.post(url, params={"key": api_key}, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        results = json.loads(text)
    except Exception as e:
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


def score_all(records, interests, api_key, model, batch_size=8, pause_seconds=10):
    scored = []
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        scored.extend(score_batch(batch, interests, api_key, model))
        if i + batch_size < len(records):
            time.sleep(pause_seconds)  # marge pour rester sous les limites du tier gratuit
    return scored


# ───────────────────────── Digest & envoi ─────────────────────────

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
    seen_ids = load_seen_ids()

    all_records = []
    for kw in config["keywords"]:
        all_records += fetch_openalex(
            kw, since_date, config["max_results_per_query"], config.get("contact_email"),
            os.environ["OPENALEX_API_KEY"],
        )
        if config.get("arxiv_categories"):
            all_records += fetch_arxiv(kw, config["arxiv_categories"], config["max_results_per_query"])

    # sécurité supplémentaire : on ne garde que les papiers dans la fenêtre demandée
    all_records = [r for r in all_records if r["date"] >= since_date]

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
    html = build_digest_html(top, period_label)

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
