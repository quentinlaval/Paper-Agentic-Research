# Veille scientifique automatisée

Récupère chaque semaine les nouveaux articles (OpenAlex + arXiv) sur vos
mots-clés, les fait trier/résumer par Gemini (gratuit), et vous envoie
un email avec les plus pertinents. Tourne entièrement dans GitHub
Actions : rien à installer sur votre machine, aucun droit administrateur
nécessaire.

## Mise en place (10 minutes)

### 1. Créer le repo
Créez un nouveau repo GitHub (public de préférence : les minutes
GitHub Actions y sont illimitées et gratuites ; en privé vous avez
2000 min/mois gratuites, largement suffisant ici). Poussez-y ces fichiers.

### 2. Récupérer une clé Gemini gratuite
Allez sur https://aistudio.google.com/apikey, connectez-vous avec un
compte Google, cliquez sur "Create API key". C'est gratuit, sans carte
bancaire, avec un quota quotidien qui suffit largement pour une veille
hebdomadaire (voir la section "Limites" plus bas).

### 3. Créer un mot de passe d'application Gmail (pour l'envoi d'email)
Le script envoie l'email via SMTP Gmail. Gmail exige un "mot de passe
d'application" (pas votre mot de passe habituel) :
1. Activez la validation en deux étapes sur votre compte Google si ce
   n'est pas déjà fait : https://myaccount.google.com/security
2. Générez un mot de passe d'application ici :
   https://myaccount.google.com/apppasswords
3. Gardez ce mot de passe de côté, vous en aurez besoin à l'étape suivante.

(Vous utilisez un autre fournisseur qu'Gmail ? Remplacez simplement
`smtp.gmail.com` / le port dans `send_email()` de `src/main.py` par les
paramètres SMTP de votre fournisseur.)

### 4. Ajouter les secrets dans le repo GitHub
Dans votre repo : **Settings → Secrets and variables → Actions → New
repository secret**, ajoutez :

| Nom du secret     | Valeur                                             |
|--------------------|-----------------------------------------------------|
| `GEMINI_API_KEY`  | la clé obtenue à l'étape 2                          |
| `OPENALEX_API_KEY`| clé gratuite créée sur openalex.org/settings/api    |
| `EMAIL_USER`      | votre adresse Gmail expéditrice                     |
| `EMAIL_PASS`      | le mot de passe d'application généré à l'étape 3    |
| `EMAIL_TO`        | l'adresse qui recevra le digest (peut être la même) |

### 5. Personnaliser `config.yaml`
Modifiez `interests`, `keywords` et `arxiv_categories` selon votre
domaine. C'est le seul fichier à éditer pour changer le sujet de la veille.

### 6. Tester
Onglet **Actions** de votre repo → sélectionnez "Veille scientifique" →
**Run workflow** (déclenchement manuel, ne nécessite pas d'attendre le
cron). Regardez les logs de l'étape "Générer et envoyer le digest" pour
vérifier que tout fonctionne, puis regardez votre boîte mail.

Le cron par défaut est réglé sur tous les lundis à 7h UTC — modifiable
dans `.github/workflows/paper-digest.yml`.

### 7. Activer le site GitHub Pages
**Settings → Pages** → Source : "Deploy from a branch" → Branch : `main`,
dossier `/docs` → **Save**. Le site (page d'accueil + une page par semaine,
articles retenus/envoyés en vert) se met à jour automatiquement à chaque
run, à l'adresse `https://<votre-user>.github.io/<votre-repo>/`.

## Comment ça marche

1. **Récupération** : pour chaque mot-clé, interroge l'API OpenAlex
   (gratuite, sans clé, agrège arXiv/PubMed/Crossref/etc. avec résumé)
   et, si `arxiv_categories` est renseigné, l'API arXiv en direct pour
   les tout derniers preprints.
2. **Déduplication** : contre `seen_ids.json` (mémoire persistante entre
   les runs, committée automatiquement à chaque exécution) + contre les
   titres en double dans le même run.
3. **Filtre mots-clés** : sécurité gratuite avant d'appeler le LLM —
   élimine les faux positifs de la recherche plein texte.
4. **Scoring LLM** : les résumés restants sont envoyés à Gemini par
   lots (8 par appel) qui renvoie, pour chacun, un score de pertinence
   0-10, un résumé en français et une justification — en un seul appel
   structuré (JSON) au lieu de deux étapes séparées.
5. **Digest** : les articles au-dessus de `min_score`, triés par score,
   limités à `top_n`, sont mis en forme et envoyés par email.
6. Tous les articles examinés (retenus ou non) sont marqués comme "vus"
   pour ne jamais les analyser deux fois.

## Limites du tier gratuit

- **Gemini API** : le tier gratuit tourne autour de 10-15 requêtes/minute
  et un quota quotidien de requêtes selon le modèle. Avec 3 mots-clés et
  un `max_results_per_query` de 15, vous êtes très large pour un run
  hebdomadaire. Si vous élargissez beaucoup (dizaines de mots-clés), 
  augmentez `pause_seconds` dans `score_all()` ou réduisez `batch_size`.
- **OpenAlex** : ~100 000 requêtes/jour en pool "polie" (avec `contact_email`
  renseigné) — non limitant ici.
- **arXiv** : pas de clé requise, usage raisonnable attendu (quelques
  requêtes espacées, ce qui est le cas ici).
