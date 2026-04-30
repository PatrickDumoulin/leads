import os
import io
import re
import json
import time
import uuid
import hmac
import hashlib
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests as req_lib
import pandas as pd
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context, session, redirect
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
import anthropic

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.secret_key = 'sk-leadlists-8f3k2p9x7m'
APP_PASSWORD = 'Nw2r#JajyeKr'
DEPLOY_SECRET = 'D9kP2mX7rL4nQ8tY3vB6jH1cF5sZ0wR'

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PID_FILE = os.path.join(_BASE_DIR, 'server.pid')
with open(_PID_FILE, 'w') as _f:
    _f.write(str(os.getpid()))

_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Leadlist Tools — Connexion</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f0f13; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
    .card { background: #1a1a22; border: 1px solid #2a2a38; border-radius: 14px; padding: 40px; width: 100%; max-width: 360px; }
    h1 { font-size: 1.4rem; font-weight: 700; color: #fff; margin-bottom: 4px; }
    p { color: #888; font-size: 0.85rem; margin-bottom: 24px; }
    label { display: block; font-size: 0.8rem; color: #888; margin-bottom: 6px; }
    input { width: 100%; padding: 10px 14px; background: #22222e; border: 1px solid #2e2e40; border-radius: 8px; color: #e0e0e0; font-size: 0.9rem; outline: none; }
    input:focus { border-color: #6c63ff; }
    button { width: 100%; padding: 12px; background: #6c63ff; color: #fff; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; margin-top: 16px; }
    button:hover { background: #5a52e0; }
    .error { color: #e05555; font-size: 0.82rem; margin-top: 12px; text-align: center; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Leadlist Tools</h1>
    <p>by Sonoria</p>
    <form method="POST">
      <label>Mot de passe</label>
      <input type="password" name="password" autofocus autocomplete="current-password">
      <button type="submit">Entrer</button>
      {% if error %}<div class="error">Mot de passe incorrect.</div>{% endif %}
    </form>
  </div>
</body>
</html>'''


@app.before_request
def check_auth():
    if request.endpoint in ('login', 'static', 'deploy_webhook'):
        return
    if not session.get('authenticated'):
        if request.method != 'GET':
            return jsonify({'error': 'Session expirée. Veuillez recharger la page et vous reconnecter.'}), 401
        return redirect('/login')


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = False
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['authenticated'] = True
            return redirect('/')
        error = True
    from flask import render_template_string
    return render_template_string(_LOGIN_HTML, error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

RESULTS_DIR = os.path.join(os.path.dirname(__file__), '.job_results')
os.makedirs(RESULTS_DIR, exist_ok=True)

DB_PATH = os.path.join(os.path.dirname(__file__), 'leads.db')


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS lists (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            table_name   TEXT NOT NULL,
            created_date TEXT NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS contacted_leads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            linkedin_url TEXT,
            email        TEXT,
            name         TEXT,
            company      TEXT,
            added_date   TEXT NOT NULL,
            list_id      INTEGER REFERENCES lists(id)
        )''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_linkedin
            ON contacted_leads(linkedin_url)
            WHERE linkedin_url IS NOT NULL AND linkedin_url != ""''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_email
            ON contacted_leads(email)
            WHERE email IS NOT NULL AND email != ""''')
        conn.execute('''CREATE TABLE IF NOT EXISTS blacklist (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            linkedin_url TEXT,
            email        TEXT,
            name         TEXT,
            company      TEXT,
            added_date   TEXT NOT NULL,
            list_id      INTEGER REFERENCES lists(id)
        )''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_bl_linkedin
            ON blacklist(linkedin_url)
            WHERE linkedin_url IS NOT NULL AND linkedin_url != ""''')
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_bl_email
            ON blacklist(email)
            WHERE email IS NOT NULL AND email != ""''')
        # Migrations for existing DBs without list_id column
        for tbl in ('contacted_leads', 'blacklist'):
            try:
                conn.execute(f'ALTER TABLE {tbl} ADD COLUMN list_id INTEGER REFERENCES lists(id)')
            except sqlite3.OperationalError:
                pass
        conn.commit()


init_db()


def _db_get_known_sets():
    """Returns (contacted_li, contacted_em, blacklist_li, blacklist_em) as lowercase sets."""
    with sqlite3.connect(DB_PATH) as conn:
        c_li = {r[0].strip().lower() for r in conn.execute(
            "SELECT linkedin_url FROM contacted_leads WHERE linkedin_url IS NOT NULL AND linkedin_url != ''"
        ).fetchall()}
        c_em = {r[0].strip().lower() for r in conn.execute(
            "SELECT email FROM contacted_leads WHERE email IS NOT NULL AND email != ''"
        ).fetchall()}
        b_li = {r[0].strip().lower() for r in conn.execute(
            "SELECT linkedin_url FROM blacklist WHERE linkedin_url IS NOT NULL AND linkedin_url != ''"
        ).fetchall()}
        b_em = {r[0].strip().lower() for r in conn.execute(
            "SELECT email FROM blacklist WHERE email IS NOT NULL AND email != ''"
        ).fetchall()}
    return c_li, c_em, b_li, b_em


def _is_in_sets(li_val, em_val, li_set, em_set):
    li = li_val.strip().lower() if li_val else ''
    em = em_val.strip().lower() if em_val else ''
    return (li and li in li_set) or (em and em in em_set)


def db_add_df(df, table='contacted_leads', list_id=None):
    """Adds leads to the given table. Returns (added, skipped)."""
    c_li, c_em, b_li, b_em = _db_get_known_sets()
    existing_li = c_li if table == 'contacted_leads' else b_li
    existing_em = c_em if table == 'contacted_leads' else b_em
    added, skipped = 0, 0
    now = datetime.now().isoformat(timespec='seconds')

    with sqlite3.connect(DB_PATH) as conn:
        for _, row in df.iterrows():
            li = _s(row.get('linkedin_url')).lower()
            em = _s(row.get('email')).lower()
            if not li and not em:
                skipped += 1
                continue
            if _is_in_sets(li, em, existing_li, existing_em):
                skipped += 1
                continue
            try:
                conn.execute(
                    f'INSERT OR IGNORE INTO {table} (linkedin_url, email, name, company, added_date, list_id) VALUES (?, ?, ?, ?, ?, ?)',
                    (li or None, em or None, _s(row.get('name')), _s(row.get('company')), now, list_id)
                )
                if conn.execute('SELECT changes()').fetchone()[0] > 0:
                    added += 1
                    if li:
                        existing_li.add(li)
                    if em:
                        existing_em.add(em)
                else:
                    skipped += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
    return added, skipped


def db_filter_df(df, exclude_contacted=True, exclude_blacklist=True):
    """Returns df with contacted/blacklisted leads removed."""
    c_li, c_em, b_li, b_em = _db_get_known_sets()
    li_col = df['linkedin_url'] if 'linkedin_url' in df.columns else pd.Series([''] * len(df), index=df.index)
    em_col = df['email'] if 'email' in df.columns else pd.Series([''] * len(df), index=df.index)

    def is_excluded(i):
        li = _s(li_col.iloc[i])
        em = _s(em_col.iloc[i])
        if exclude_contacted and _is_in_sets(li, em, c_li, c_em):
            return True
        if exclude_blacklist and _is_in_sets(li, em, b_li, b_em):
            return True
        return False

    mask = pd.Series([is_excluded(i) for i in range(len(df))])
    return df[~mask.values].reset_index(drop=True)


def db_stats():
    with sqlite3.connect(DB_PATH) as conn:
        c_total = conn.execute('SELECT COUNT(*) FROM contacted_leads').fetchone()[0]
        c_last  = conn.execute('SELECT MAX(added_date) FROM contacted_leads').fetchone()[0]
        b_total = conn.execute('SELECT COUNT(*) FROM blacklist').fetchone()[0]
        b_last  = conn.execute('SELECT MAX(added_date) FROM blacklist').fetchone()[0]
    return {
        'contacted_total': c_total,
        'contacted_last': c_last,
        'blacklist_total': b_total,
        'blacklist_last': b_last,
    }


# In-memory job store: job_id -> {events: [], done: bool}
_jobs = {}
_jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Column mapping (Combine tab)
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = {
    'name':                          ['name', 'person_name', 'personname', 'full name', 'fullname', 'contact name'],
    'domain_name':                   ['domain_name', 'domainname', 'domain', 'website domain'],
    'person_name':                   ['person_name', 'personname', 'full name', 'name'],
    'result_title':                  ['result_title', 'resulttitle', 'result title'],
    'email':                         ['email', 'email address', 'e-mail', 'courriel'],
    'first_name':                    ['first name', 'firstname', 'first_name', 'prénom', 'prenom'],
    'last_name':                     ['last name', 'lastname', 'last_name', 'nom de famille', 'surname'],
    'summary':                       ['summary', 'bio', 'about', 'description personnelle'],
    'job_title':                     ['job title', 'jobtitle', 'job_title', 'titre du poste', 'poste occupé'],
    'job_description':               ['job description', 'jobdescription', 'job_description', 'role description'],
    'linkedin_url':                  ['linkedin url', 'linkedinurl', 'linkedin_url', 'linkedin profile url', 'profil linkedin'],
    'location':                      ['location', 'city', 'ville', 'région', 'region', 'country'],
    'linkedin_description':          ['linkedin description', 'company description', 'linkedin_description'],
    'linkedin_specialities':         ['linkedin specialities', 'linkedin speciality', 'specialities', 'specialties', 'linkedin_specialities'],
    'linkedin_employees':            ['linkedin employees', 'linkedin_employees', 'employees', 'employés', 'headcount'],
    'linkedin_company_revenue_range':['linkedin company revenue range', 'revenue range', 'linkedin_company_revenue_range'],
    'language':                      ['languages', 'language', 'langue', 'langues'],
    'headline':                      ['headline', 'tagline', 'accroche'],
    'corporate_website':             ['corporate website', 'corporate_website', 'website', 'site web', 'site internet'],
    'company':                       ['company', 'compagnie', 'entreprise', 'organization', 'organisation', 'employer'],
}

IGNORE_COLUMNS = {
    'phone', 'telephone', 'tel', 'mobile', 'fax',
    'member linkedin id', 'company linkedin id',
    'valid_email_only', 'email_type', 'amf_status', 'matching filters',
    'recent duplicate', 'open profile', 'premium member', 'skills',
    'job started on', 'job ended on', 'linkedin founded year',
    'linkedin industry', 'linkedin company location', 'linkedin company employee count',
    'corporate linkedin url',
}


def normalize(s):
    return s.lower().strip().replace('_', ' ').replace('-', ' ')


def find_best_column_match(source_col, taken_targets):
    src = normalize(source_col)
    if src in IGNORE_COLUMNS:
        return None
    for target, aliases in OUTPUT_COLUMNS.items():
        if target in taken_targets:
            continue
        for alias in aliases:
            if src == normalize(alias):
                return target
    best_score, best_target = 0, None
    for target, aliases in OUTPUT_COLUMNS.items():
        if target in taken_targets:
            continue
        for alias in aliases:
            score = SequenceMatcher(None, src, normalize(alias)).ratio()
            if score > best_score:
                best_score, best_target = score, target
    return best_target if best_score >= 0.80 else None


def map_columns(df):
    mapping, taken = {}, set()
    for col in df.columns:
        target = find_best_column_match(col, taken)
        if target:
            mapping[col] = target
            taken.add(target)
    return mapping


def read_file(file):
    name = file.filename.lower()
    if name.endswith('.csv'):
        content = file.read()
        for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
            try:
                return pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str)
            except Exception:
                continue
    elif name.endswith(('.xlsx', '.xls')):
        return pd.read_excel(file, dtype=str)
    raise ValueError(f"Format non supporté: {file.filename}")


def combine_files(files):
    all_dfs, file_stats = [], []
    for f in files:
        df = read_file(f)
        col_map = map_columns(df)
        renamed = df.rename(columns=col_map)
        keep = [c for c in renamed.columns if c in OUTPUT_COLUMNS]
        renamed = renamed[keep]
        for col in OUTPUT_COLUMNS:
            if col not in renamed.columns:
                renamed[col] = ''
        renamed = renamed[list(OUTPUT_COLUMNS.keys())]
        mask = renamed['name'].isna() | (renamed['name'].str.strip() == '')
        fname = renamed['first_name'].fillna('').str.strip()
        lname = renamed['last_name'].fillna('').str.strip()
        renamed.loc[mask, 'name'] = (fname + ' ' + lname).str.strip()
        mask2 = renamed['person_name'].isna() | (renamed['person_name'].str.strip() == '')
        renamed.loc[mask2, 'person_name'] = renamed.loc[mask2, 'name']
        file_stats.append({'filename': f.filename, 'rows': len(renamed), 'columns_mapped': len(col_map)})
        all_dfs.append(renamed)

    combined = pd.concat(all_dfs, ignore_index=True).fillna('').astype(str).replace('nan', '')
    before = len(combined)

    def dedup_key(row):
        email = row['email'].strip().lower()
        if email:
            return ('email', email)
        li = row['linkedin_url'].strip().lower()
        if li:
            return ('linkedin', li)
        name = row['name'].strip().lower()
        return ('name_company', f"{name}|{row['company'].strip().lower()}")

    seen, keep_indices = {}, []
    for i, row in combined.iterrows():
        key = dedup_key(row)
        if key not in seen:
            seen[key] = i
            keep_indices.append(i)

    combined = combined.loc[keep_indices].reset_index(drop=True)
    return combined, file_stats, before, len(combined)


# ---------------------------------------------------------------------------
# Web scraping
# ---------------------------------------------------------------------------

SCRAPE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
    'Accept-Language': 'fr-CA,fr;q=0.9,en;q=0.8',
}


def scrape_website(url, timeout=8):
    if not url or not url.strip():
        return None
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    try:
        resp = req_lib.get(url, headers=SCRAPE_HEADERS, timeout=timeout, allow_redirects=True, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Remove clutter
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form', 'iframe']):
            tag.decompose()

        parts = []

        title = soup.find('title')
        if title:
            parts.append(title.get_text(strip=True))

        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            parts.append(meta_desc['content'].strip())

        for tag in soup.find_all(['h1', 'h2', 'h3']):
            t = tag.get_text(strip=True)
            if t and len(t) < 200:
                parts.append(t)

        main = soup.find('main') or soup.find(id='main') or soup.find(class_='main') or soup.body
        if main:
            for p in main.find_all('p'):
                t = p.get_text(strip=True)
                if len(t) > 40:
                    parts.append(t)
                if len('\n'.join(parts)) > 3000:
                    break

        text = '\n'.join(dict.fromkeys(parts))
        return text[:3000] if text.strip() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Icebreaker generation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un expert en copywriting B2B francophone qui rédige des icebreakers pour des emails de prospection froide.

Le but: que le destinataire croie que l'expéditeur a vraiment visité leur site et fait une vraie recherche.

FORMAT OBLIGATOIRE - 1 seule phrase:
Commence TOUJOURS par "Très intéressant" suivi SANS virgule d'une construction naturelle en français, par exemple:
   - "Très intéressant le fait que [entreprise] [fait quelque chose de spécifique]."
   - "Très intéressant ce focus sur [domaine spécifique] chez [entreprise]."
   - "Très intéressant de voir [entreprise] [verbe + détail concret]."

RÈGLES:
- Si le contenu du site web est disponible, utilise-le en priorité. Si non disponible, utilise la description LinkedIn, les spécialités et le titre de poste pour générer l'icebreaker. Tu génères TOUJOURS la phrase, peu importe les données disponibles. Ne dis JAMAIS que tu n'as pas accès au site ou que les données sont insuffisantes.
- Sois CONCRET: cite un produit, un marché servi, une techno, un client type, un chiffre, une certification
- Phrase courte, ton naturel - comme si tu l'avais tapé rapidement entre deux réunions
- INTERDIT TOTAL ET ABSOLU: le caractère tiret long (le caractère Unicode U+2014). Ne l'utilise jamais, sous aucun prétexte.
- INTERDIT TOTAL ET ABSOLU: le caractère tiret moyen (le caractère Unicode U+2013). Ne l'utilise jamais.
- Utilise uniquement le trait d'union ordinaire (-) si tu as besoin d'un tiret.
- INTERDIT: virgule après "Très intéressant", points-virgules, parenthèses, "innovant", "vision", "expertise", "passionné", "fier"
- Retourne UNIQUEMENT cette phrase, rien d'autre"""

SYSTEM_PROMPT_EN = """You are a B2B copywriting expert writing icebreakers for cold email prospecting.

Goal: make the recipient believe the sender genuinely visited their website and did real research.

MANDATORY FORMAT — 1 sentence only:
Always start with "Very interesting" followed WITHOUT a comma by a natural English construction, for example:
   - "Very interesting that [company] [does something specific]."
   - "Very interesting how [company] focuses on [specific domain]."
   - "Very interesting to see [company] [verb + concrete detail]."

RULES:
- If website content is available, prioritize it. If not, use LinkedIn description, specialties and job title. You ALWAYS generate the sentence regardless of available data. NEVER say you don't have access to the site or that data is insufficient.
- Be CONCRETE: mention a product, a market served, a technology, a client type, a number, a certification
- Short sentence, natural tone — as if typed quickly between two meetings
- ABSOLUTELY FORBIDDEN: em dash (U+2014). Never use it under any circumstance.
- ABSOLUTELY FORBIDDEN: en dash (U+2013). Never use it.
- Use only a regular hyphen (-) if you need a dash.
- FORBIDDEN: comma after "Very interesting", semicolons, parentheses, "innovative", "visionary", "expertise", "passionate", "proud"
- Return ONLY this sentence, nothing else"""


def _s(val):
    """Safely convert any value to a stripped string."""
    if val is None:
        return ''
    try:
        s = str(val).strip()
        return '' if s.lower() in ('nan', 'none', '') else s
    except Exception:
        return ''


def _normalize_accents(t):
    return (t.replace('é', 'e').replace('è', 'e').replace('ê', 'e')
             .replace('î', 'i').replace('ô', 'o').replace('û', 'u')
             .replace('à', 'a').replace('ù', 'u'))


# ---------------------------------------------------------------------------
# Accroche personnalisée par titre
# ---------------------------------------------------------------------------


# Non-decision-maker roles to exclude entirely from icebreaker output
_BAD_ROLE_KEYWORDS = (
    # IT / numérique
    'information technology', 'informatique', 'it director', 'director of it',
    'it manager', 'software development', 'software engineer', 'software manager',
    'software associate', 'developpement logiciel', 'digital strategy', 'head of digital',
    'chef service', 'systemes numeriques', 'technologie numerique',
    'chief information officer', ' cio', 'group cio', 'administrateur ti',
    'chief technology officer', ' cto', 'system network', 'network admin',
    # Innovation / R&D (pas le bon angle)
    'innovation', 'charge de projet', 'chargé de projet', 'r&d',
    # Marketing / ventes / finance (pas le bon interlocuteur)
    'marketing', 'sales consultant', 'sales manager', 'retail sales', 'retail market',
    'brand', 'communication', 'ventes corporatives', 'representant', 'representante',
    'account executive', 'sales representative', 'conseiller aux ventes',
    'conseillere aux ventes', 'courtier', 'financement',
    'directeur marche', 'directeur du marche', 'directeur commercial',
    'business development', 'developpement des affaires', 'developpement affaires',
    'vp ventes', 'vice-president ventes', 'vice president ventes',
    # Contributeurs individuels / non-décideurs
    'ingenieur ', 'ingenieure', 'engineer ', 'specialist', 'specialiste',
    'coordinateur', 'coordonnateur', 'coordinator', 'coordinatrice', 'coordonnatrice',
    'journalier', 'technicien', 'technician', 'analyste', 'analyst',
    'consultant', 'electromecanicien', 'electricien', 'dessinateur', 'concepteur',
    'machine operator', 'operatrice', 'operateur', 'foreman', 'contremaitre',
    'retraite', 'yoga', 'accountant', 'comptable', 'auditeur', 'program management',
    'professeur', 'enseignant', 'formateur', 'etudiant', 'pharmacien', 'cuisinier',
    'fondeur', 'chemist', 'designer', 'collaborator', 'activator', 'healing',
    'team leader', 'project leader',
)


def is_bad_role(job_title):
    if not job_title:
        return False
    t = _normalize_accents(job_title.lower().strip()) + ' '
    return any(x in t for x in _BAD_ROLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Accroche procédures (template rule-based, aucun appel API)
# ---------------------------------------------------------------------------

_FEM_FIRST_NAMES = frozenset([
    'marie', 'julie', 'sophie', 'isabelle', 'anne', 'catherine', 'nathalie',
    'caroline', 'valerie', 'jennifer', 'jessica', 'stephanie', 'amelie',
    'emilie', 'sarah', 'sara', 'laura', 'lucie', 'claire', 'dominique',
    'martine', 'sylvie', 'michele', 'helene', 'veronique', 'virginie',
    'audrey', 'celine', 'nadine', 'patricia', 'karine', 'france', 'francine',
    'louise', 'monique', 'diane', 'linda', 'manon', 'maude', 'fanny',
    'genevieve', 'florence', 'alexandra', 'alice', 'charlotte', 'delphine',
    'elodie', 'francoise', 'gabrielle', 'joelle', 'josee', 'laure',
    'laurence', 'lea', 'lise', 'magali', 'marianne', 'marine', 'marlene',
    'mathilde', 'melanie', 'melissa', 'mireille', 'muriel', 'nadia', 'nancy',
    'noemie', 'olivia', 'pascale', 'pauline', 'rachel', 'renee', 'roxane',
    'samantha', 'sandrine', 'sonia', 'suzanne', 'vanessa', 'yasmine', 'zoe',
    'amanda', 'amy', 'ashley', 'carol', 'deborah', 'donna', 'elizabeth',
    'emma', 'hannah', 'helen', 'jane', 'janet', 'joanna', 'julia', 'karen',
    'kate', 'katherine', 'kelly', 'kim', 'kimberly', 'lauren', 'leah',
    'lily', 'lisa', 'margaret', 'mary', 'megan', 'michelle', 'molly',
    'natalie', 'pamela', 'paula', 'rebecca', 'rose', 'ruth', 'sandra',
    'sharon', 'susan', 'tamara', 'tanya', 'tara', 'tracy', 'victoria',
    'brittany', 'heather', 'holly', 'jasmine', 'jenna', 'raef',
])

_FEM_TITLE_MARKERS = (
    'directrice', 'presidente', 'vice-presidente', 'dirigeante',
    'associee', 'fondatrice', 'co-fondatrice', 'pdge',
)


def _is_feminine(job_title, first_name=''):
    t = _normalize_accents((job_title or '').lower())
    if any(m in t for m in _FEM_TITLE_MARKERS):
        return True
    fn = _normalize_accents((first_name or '').lower().split()[0]) if first_name else ''
    return fn in _FEM_FIRST_NAMES


def _build_accroche_procedures(job_title, first_name='', lang='fr'):
    t = _normalize_accents((job_title or '').lower()) + ' '
    if lang == 'en':
        is_exec = any(x in t for x in (
            'president', 'ceo ', 'pdg ', 'vp ', 'vice-president', 'vice president',
            'chief ', 'coo ', 'cfo ', 'proprietaire', 'actionnaire',
            'associe ', 'associee', 'fondateur', 'fondatrice', 'partner ',
            'partenaire', 'owner ',
        ))
        if is_exec:
            return ("What led me to reach out: as a leader, you've probably built your company on the expertise of a few key people, "
                    "but where does that knowledge go if they leave tomorrow?")
        if any(x in t for x in ('qualite ', 'quality ', 'conformite', 'compliance', ' qa ', ' qc ')):
            return ("What led me to reach out: as Quality Manager, you know that most non-conformities don't come from lack of skill — "
                    "they come from information not being in the right place at the right time.")
        if any(x in t for x in ('ingenierie', 'engineering', 'technique ', 'technical')):
            return ("What led me to reach out: as Engineering Manager, your best experts probably spend too much time answering "
                    "the same operational questions instead of solving problems that actually matter.")
        if any(x in t for x in ('maintenance', 'fiabilite', 'reliability')):
            return ("What led me to reach out: as Maintenance Manager, you've probably lived this scenario: a line goes down, "
                    "and the only technician who knows what to do is unreachable.")
        if any(x in t for x in ('fabrication', 'manufacturing', 'manufactur')):
            return ("What led me to reach out: as Manufacturing Manager, you've probably noticed that the same procedure is executed "
                    "differently depending on who's on shift, and nobody really addresses it.")
        if 'production' in t:
            return ("What led me to reach out: as Production Manager, you know exactly what it costs when an operator stops their line "
                    "to find an answer that nobody can locate.")
        if any(x in t for x in ('amelioration continue', 'lean ', 'excellence operationnelle', 'kaizen', 'continuous improvement')):
            return ("What led me to reach out: as Continuous Improvement Manager, you know the biggest barrier to performance "
                    "is rarely the machines — it's know-how that isn't accessible at the right time and place.")
        if any(x in t for x in ('operation', 'operatrice')):
            return ("What led me to reach out: as Operations Manager, you see the numbers, but the 5 to 10 hours lost per employee "
                    "per week searching for information — nobody really measures that.")
        if any(x in t for x in ('usine', 'plant ')):
            return ("What led me to reach out: as Plant Manager, you carry a risk few people see: the day your best expert leaves, "
                    "their knowledge leaves with them.")
        if any(x in t for x in ('logistique', 'supply chain', 'approvisionnement', 'achats ')):
            return ("What led me to reach out: as Supply Chain Manager, you know exactly what it costs when a poorly followed procedure "
                    "slows the entire chain and nobody does it the same way.")
        if any(x in t for x in ('ressources humaines', 'human resources', ' rh ', 'formation ')):
            return ("What led me to reach out: as HR Manager in manufacturing, you absorb a cost nobody measures: a senior fully "
                    "mobilized for 3 to 6 weeks every time someone new joins.")
        if any(x in t for x in ('directeur', 'directrice', 'director', 'gestionnaire', 'manager ', 'responsable')):
            return ("What led me to reach out: as a Manager, you probably carry a risk few people in your organization see: "
                    "the day a key employee leaves, their knowledge leaves with them.")
        return ("What led me to reach out: as a leader, you've probably built your company on the expertise of a few key people, "
                "but where does that knowledge go if they leave tomorrow?")

    fem = _is_feminine(job_title, first_name)
    dir_e = 'directrice' if fem else 'directeur'
    dirig = 'dirigeante' if fem else 'dirigeant'

    # Senior exec — checked first so VP Manufacturing → dirigeant, pas fabrication
    is_exec = any(x in t for x in (
        'president', 'ceo ', 'pdg ', 'vp ', 'vice-president', 'vice president',
        'chief ', 'coo ', 'cfo ', 'proprietaire', 'actionnaire',
        'associe ', 'associee', 'fondateur', 'fondatrice', 'partner ',
        'partenaire', 'owner ',
    ))
    if is_exec:
        return (f"Ce qui m'a amené à vous écrire : En tant que {dirig}, vous avez probablement "
                "bâti votre entreprise sur le savoir-faire de quelques personnes clés, mais ce "
                "savoir, il est où si elles partent demain ?")

    # Qualité
    if any(x in t for x in ('qualite ', 'quality ', 'conformite', 'compliance', ' qa ', ' qc ')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} qualité, vous savez que "
                "la plupart des non-conformités ne viennent pas d'un manque de compétence, elles "
                "viennent d'une information qui n'était pas au bon endroit au bon moment.")

    # Ingénierie / Technique
    if any(x in t for x in ('ingenierie', 'engineering', 'technique ', 'technical')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} d'ingénierie, vos meilleurs "
                "experts passent probablement trop de temps à répondre aux mêmes questions "
                "opérationnelles au lieu de résoudre des problèmes qui comptent vraiment.")

    # Maintenance
    if any(x in t for x in ('maintenance', 'fiabilite', 'reliability')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} maintenance, vous avez "
                "probablement déjà vécu le scénario : une ligne tombe, et le seul technicien qui "
                "sait quoi faire est introuvable.")

    # Fabrication / Manufacturing (avant production pour éviter faux-positifs)
    if any(x in t for x in ('fabrication', 'manufacturing', 'manufactur')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} de fabrication, vous avez "
                "probablement remarqué que la même procédure est exécutée différemment selon qui "
                "est en poste, et parfois personne ne s'en préoccupe vraiment.")

    # Production
    if 'production' in t:
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} de production, vous savez "
                "exactement ce que ça coûte quand un opérateur arrête sa ligne pour aller chercher "
                "une réponse que personne ne trouve.")

    # Amélioration continue / Lean
    if any(x in t for x in ('amelioration continue', 'lean ', 'excellence operationnelle', 'kaizen')):
        return ("Ce qui m'a amené à vous écrire : En tant que responsable amélioration continue, "
                "vous savez que le plus grand frein à la performance c'est rarement les machines, "
                "c'est le savoir-faire qui n'est pas accessible au bon moment au bon endroit.")

    # Opérations (après fabrication/production)
    if any(x in t for x in ('operation', 'operatrice')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} des opérations, vous voyez "
                "les chiffres, mais les 5 à 10 heures perdues par employé par semaine à chercher "
                "de l'information, personne ne les mesure vraiment.")

    # Usine / Plant
    if any(x in t for x in ('usine', 'plant ')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e} d'usine, vous portez un "
                "risque que peu de gens voient : le jour où votre meilleur expert part, son savoir "
                "part avec lui.")

    # Logistique / Supply chain
    if any(x in t for x in ('logistique', 'supply chain', 'approvisionnement', 'achats ')):
        return ("Ce qui m'a amené à vous écrire : En tant que responsable logistique, vous savez "
                "exactement ce que ça coûte quand une procédure mal suivie ralentit toute la chaîne "
                "et que personne n'a la même façon de faire.")

    # RH
    if any(x in t for x in ('ressources humaines', 'human resources', ' rh ', 'formation ')):
        return ("Ce qui m'a amené à vous écrire : En tant que responsable RH dans le manufacturier, "
                "vous absorbez un coût que personne ne mesure : un senior mobilisé à temps plein "
                "pendant 3 à 6 semaines chaque fois qu'un nouveau arrive.")

    # Directeur générique
    if any(x in t for x in ('directeur', 'directrice', 'director', 'gestionnaire', 'manager ', 'responsable')):
        return (f"Ce qui m'a amené à vous écrire : En tant que {dir_e}, vous portez probablement "
                "un risque que peu de gens dans votre organisation voient : le jour où un employé "
                "clé part, son savoir part avec lui.")

    # Défaut (senior sans titre reconnu)
    return (f"Ce qui m'a amené à vous écrire : En tant que {dirig}, vous avez probablement bâti "
            "votre entreprise sur le savoir-faire de quelques personnes clés, mais ce savoir, il "
            "est où si elles partent demain ?")


_CERT_PATTERNS = [
    (r'\bISO\s*9001\b',    'ISO 9001'),
    (r'\bISO\s*13485\b',   'ISO 13485'),
    (r'\bISO\s*14001\b',   'ISO 14001'),
    (r'\bISO\s*22000\b',   'ISO 22000'),
    (r'\bISO\s*45001\b',   'ISO 45001'),
    (r'\bAS\s*9100\b',     'AS9100'),
    (r'\bIATF\s*16949\b',  'IATF 16949'),
    (r'\bSQF\b',           'SQF'),
    (r'\bHACCP\b',         'HACCP'),
    (r'\bFSSC\s*22000\b',  'FSSC 22000'),
    (r'\bBRCGS\b',         'BRC/BRCGS'),
    (r'\bBRC\b',           'BRC/BRCGS'),
    (r'\bBPF\b',           'BPF'),
    (r'\bGMP\b',           'GMP/BPF'),
    (r'\bCNESST\b',        'CNESST'),
]


def detect_existing_certs(web_content, linkedin_spec, linkedin_desc):
    text = ' '.join(filter(None, [web_content or '', linkedin_spec or '', linkedin_desc or '']))
    found, seen = [], set()
    for pattern, name in _CERT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE) and name not in seen:
            found.append(name)
            seen.add(name)
    return found


COMBINED_SYSTEM_PROMPT = """Tu analyses le profil d'un prospect industriel québécois pour Sonoria, un outil qui automatise la détection de non-conformités normatives.

TÂCHE 1 — DÉTECTER LE SECTEUR :
Sonoria automatise la détection de non-conformités normatives. Une entreprise est une CIBLE si elle gère des certifications documentées (ISO, SQF, BPF, CNESST, AS9100, IATF, etc.), peu importe son secteur. En cas de doute, utilise TOUJOURS generique — jamais hors_cible.

Choisis UN seul code parmi ces six :
- agroalimentaire : transformation alimentaire industrielle, ferme de production, boulangerie commerciale, fromagerie, abattoir, brasserie industrielle, laiterie, conserverie, etc. (NON : restaurant, bar, épicerie fine, traiteur, café)
- manufacturing : fabrication, usinage, métallurgie, soudure, assemblage, électronique industrielle, semi-conducteurs, mécanique, aluminium, acier, plastiques, caoutchouc, fonderie, forge, outillage, machinerie industrielle, ventilation industrielle, pharmaceutique de fabrication (BPF/GMP, ISO 13485), équipements médicaux fabriqués, automatisation industrielle, contrôleurs industriels, etc.
- construction : bâtiment, génie civil, chantier, rénovation, excavation, toiture, plomberie, maçonnerie, charpente, infrastructure, entrepreneur général, etc.
- distribution : logistique, entreposage, transport de marchandises, chaîne d'approvisionnement industrielle, grossiste, fret, entrepôt, 3PL, etc. (NON : retail, vente au détail)
- generique : toute entreprise qui pourrait avoir une certification normative mais dont le secteur est flou ou mixte — services avec ISO 9001, laboratoires, génie-conseil technique, télécommunications, énergie, environnement, TI industriel, santé avec dispositifs certifiés, etc. En cas de doute, choisis generique.
- hors_cible : UNIQUEMENT les entreprises où il est CERTAIN qu'aucune certification normative n'existe — bar, restaurant, café, épicerie de détail, galerie d'art, cabinet d'avocats, agence immobilière, firme de finance/investissement, agence marketing/communications, école/université sans volet industriel, artiste, influenceur. Si le moindre doute existe, utilise generique. ATTENTION : une pharmacie de fabrication (Medisca, etc.) ou un fabricant de dispositifs médicaux EST dans manufacturing, pas hors_cible.

TÂCHE 2 — IDENTIFIER LA NORME LA PLUS PERTINENTE :
En fonction du secteur ET des informations spécifiques sur l'entreprise (produits, marchés servis, certifications mentionnées), choisis la norme normative la plus précise et la plus crédible pour cette entreprise. Si le secteur est hors_cible, écris "N/A".

Référence par sous-secteur :
- agroalimentaire : SQF (défaut pour toute entreprise québécoise), HACCP (si petite entreprise ou mention explicite), FSSC 22000 (si mention GFSI ou grand groupe alimentaire multinational), BRC/BRCGS (UNIQUEMENT si mention explicite d'export vers détaillants européens — Tesco, Sainsbury's, etc. ; ne pas choisir BRC pour une entreprise québécoise sans preuve claire)
- manufacturing général : ISO 9001
- manufacturing pharmaceutique / nutraceutique / chimie fine : BPF (Bonnes Pratiques de Fabrication)
- manufacturing dispositifs médicaux / équipements de santé : ISO 13485
- manufacturing aérospatial / défense / aviation : AS9100
- manufacturing automobile / équipementiers auto : IATF 16949
- construction : CNESST (toujours — c'est la norme de conformité documentaire en construction au Québec)
- distribution alimentaire : SQF ou ISO 22000
- distribution non-alimentaire : ISO 9001
- generique : conformité normative

En cas de doute, applique la norme par défaut du secteur.

TÂCHE 3 — GÉNÉRER L'ACCROCHE :
Si le secteur est hors_cible, écris exactement "N/A" comme accroche (pas d'autre texte).
Sinon, génère une seule phrase de cold email en utilisant la norme identifiée à la TÂCHE 2. Structure :
"La raison pour laquelle je vous contacte : en tant que [rôle en français] dans [secteur en français], [douleur concrète avec la norme]."

PRÉCISION : Si des produits, marchés ou procédés spécifiques sont mentionnés dans les données (spécialités LinkedIn, contenu web, description de poste), intègre-les dans la douleur. Ex : "si vos procédures de fabrication de pompes hydrauliques concordent avec ISO 9001" plutôt que "si vos procédures concordent".

DÉJÀ CERTIFIÉ : Si une note indique que l'entreprise est déjà certifiée [X], adapte l'angle : "maintenir votre certification [X] à jour exige une révision manuelle que votre équipe doit refaire chaque année" plutôt que "vérifier si vos procédures concordent".

LOCALISATION : Si la localisation est fournie, intègre-la optionnellement (ex : "dans le secteur manufacturier à Laval"). N'invente jamais de localisation si elle n'est pas dans les données.

TÂCHE 4 — IDENTIFIER LE SOUS-SECTEUR :
Si le secteur est hors_cible, agroalimentaire ou generique, écris "N/A".
Sinon, fournis en 2-5 mots le qualificatif précis du sous-secteur, tel qu'il s'insèrera dans "pour des entreprises [SOUS_SECTEUR] certifiées [NORME]".
Exemples : "aérospatiales", "automobiles", "pharmaceutiques", "de dispositifs médicaux", "de pompage industriel", "de construction", "de distribution alimentaire".
Ne jamais écrire "manufacturières" si un terme plus précis est possible.

RÈGLES STRICTES :
- Maximum 1 phrase, tout en français
- Jamais de mots anglais dans l'accroche (pas manufacturing, pas plant manager, pas CEO, etc.)
- Jamais le caractère tiret long (—) ni tiret moyen (–)
- Pas de CTA, pas de question finale
- Rôle toujours traduit en français : Plant Manager → directeur d'usine, CEO → président, Quality Manager → directeur qualité, Operations Manager → directeur des opérations, VP Operations → vice-président aux opérations, Supply Chain Manager → responsable de la chaîne logistique, General Manager → directeur général, Owner / Co-owner / Actionnaire / Co-actionnaire / Associé / Partner → propriétaire, Production Manager → directeur de production, Engineering Manager → directeur technique, QA Manager → directeur assurance qualité, Controller / Contrôleur → contrôleur, Director of Quality → directeur qualité, VP Quality → vice-président qualité
- Si le titre exact ne correspond à aucune traduction connue, utilise le titre tel quel traduit littéralement en français — ne pas inventer un titre plus senior que ce qui est indiqué
- Secteur en français dans la phrase : "dans l'agroalimentaire", "dans le secteur manufacturier", "dans la construction", "en distribution"
- La phrase se termine idéalement par "au lieu de se concentrer sur [activité principale]" ou une conséquence business concrète

ANGLE PAR RÔLE :
- Directeur qualité / assurance qualité → "votre équipe passe probablement des semaines chaque année à vérifier manuellement si vos procédures concordent avec la norme [X] au lieu de se concentrer sur l'amélioration continue."
- Directeur d'usine / directeur de production → "les semaines qui précèdent un audit [X] mobilisent probablement votre équipe qualité au détriment du rythme de production."
- Président / PDG / directeur général / propriétaire → "une non-conformité découverte lors d'un audit [X] vous coûte beaucoup plus cher qu'une détection faite à l'interne."
- Directeur des opérations / vice-président aux opérations → "votre équipe passe probablement des semaines chaque année à vérifier manuellement si vos procédures concordent avec la norme [X] au lieu de se concentrer sur les opérations."
- Directeur technique / directeur ingénierie → "s'assurer que vos procédures techniques sont conformes aux exigences [X] demande une révision manuelle que votre équipe doit refaire chaque année."
- Responsable de la chaîne logistique → "la conformité documentaire de vos procédures avec la norme [X] mobilise probablement des dizaines d'heures de votre équipe chaque année au lieu d'optimiser la chaîne."
- Titre flou ou inconnu → commencer par "dans votre entreprise, [douleur]." sans mentionner de rôle

EXEMPLES :
SECTEUR: agroalimentaire
NORME: BRC/BRCGS
ACCROCHE: La raison pour laquelle je vous contacte : en tant que directeur des opérations dans l'agroalimentaire, votre équipe passe probablement des semaines chaque année à vérifier manuellement si vos procédures concordent avec la norme BRC/BRCGS au lieu de se concentrer sur la production.
SOUS_SECTEUR: N/A

SECTEUR: manufacturing
NORME: AS9100
ACCROCHE: La raison pour laquelle je vous contacte : en tant que directeur qualité dans le secteur manufacturier, votre équipe passe probablement des semaines chaque année à vérifier manuellement si vos procédures concordent avec la norme AS9100 au lieu de se concentrer sur l'amélioration continue.
SOUS_SECTEUR: aérospatiales

SECTEUR: manufacturing
NORME: BPF
ACCROCHE: La raison pour laquelle je vous contacte : en tant que directeur qualité dans le secteur manufacturier, votre équipe passe probablement des semaines chaque année à vérifier manuellement si vos procédures concordent avec les Bonnes Pratiques de Fabrication au lieu de se concentrer sur l'amélioration continue.
SOUS_SECTEUR: pharmaceutiques

SECTEUR: construction
NORME: CNESST
ACCROCHE: La raison pour laquelle je vous contacte : en tant que président d'une entreprise de construction, une non-conformité découverte lors d'une inspection CNESST vous coûte beaucoup plus cher qu'une détection faite à l'interne.
SOUS_SECTEUR: de construction

SECTEUR: generique
NORME: conformité normative
ACCROCHE: La raison pour laquelle je vous contacte : dans votre entreprise, la révision annuelle de conformité normative mobilise probablement des dizaines d'heures de vérification manuelle chaque année au lieu de se concentrer sur votre coeur de métier.
SOUS_SECTEUR: N/A

SECTEUR: hors_cible
NORME: N/A
ACCROCHE: N/A
SOUS_SECTEUR: N/A

RÉPONDS EXACTEMENT DANS CE FORMAT (quatre lignes, rien d'autre) :
SECTEUR: [agroalimentaire|manufacturing|construction|distribution|generique|hors_cible]
NORME: [la norme spécifique ou N/A]
ACCROCHE: [la phrase ou N/A]
SOUS_SECTEUR: [le qualificatif précis ou N/A]"""


def _build_corps(secteur, norme, sous_secteur, lang='fr'):
    if secteur == 'hors_cible':
        return 'N/A'
    ss = sous_secteur if sous_secteur and sous_secteur.lower() not in ('n/a', '') else None
    if lang == 'en':
        if secteur == 'agroalimentaire':
            return (
                f"We automated compliance gap detection for {norme}-certified companies. "
                "Our client Olofée reduced their annual review from 40 hours per month to just a few hours. "
                "Their quality manager went from verifier to validator."
            )
        if ss:
            return (
                f"We automated compliance gap detection for {ss} companies certified {norme}. "
                "What used to take 40 hours per month of manual verification now takes just a few hours."
            )
        return (
            "We automated compliance gap detection for certified companies. "
            "What used to take 40 hours per month of manual verification now takes just a few hours."
        )
    if secteur == 'agroalimentaire':
        return (
            f"On a automatisé la détection des non-conformités pour des entreprises certifiées {norme}. "
            "Notre client Olofée a réduit sa révision annuelle de 40 heures par mois à quelques heures. "
            "Leur responsable qualité passe de vérificateur à validateur."
        )
    if ss:
        return (
            f"On a automatisé la détection des non-conformités pour des entreprises {ss} certifiées {norme}. "
            "Ce qui prenait 40 heures par mois de vérification manuelle se règle maintenant en quelques heures."
        )
    return (
        "On a automatisé la détection des non-conformités pour des entreprises certifiées. "
        "Ce qui prenait 40 heures par mois de vérification manuelle se règle maintenant en quelques heures."
    )


def _linkedin_data_sufficient(row):
    """True if 4/4 key LinkedIn fields are present — scraping adds little value."""
    fields = [
        _s(row.get('headline')),
        _s(row.get('linkedin_description')),
        _s(row.get('linkedin_specialities')),
        _s(row.get('job_description')),
    ]
    return sum(1 for f in fields if len(f) > 10) >= 4


def _build_user_msg(row, web_content=None):
    first_name    = _s(row.get('first_name'))
    company       = _s(row.get('company')) or "l'entreprise"
    job_title     = _s(row.get('job_title')) or _s(row.get('result_title'))
    headline      = _s(row.get('headline'))
    job_desc      = _s(row.get('job_description'))[:300]
    summary       = _s(row.get('summary'))[:200]
    linkedin_spec = _s(row.get('linkedin_specialities'))
    linkedin_desc = _s(row.get('linkedin_description'))[:400]
    location      = _s(row.get('location'))
    web_snippet   = (web_content or '')[:800]

    parts = ['Prospect :',
             f'- Prénom : {first_name}',
             f'- Entreprise : {company}',
             f'- Titre : {job_title}']
    if location:      parts.append(f'- Localisation : {location}')
    if headline:      parts.append(f'- Headline LinkedIn : {headline}')
    if job_desc:      parts.append(f'- Description du poste : {job_desc}')
    if summary:       parts.append(f'- Résumé personnel : {summary}')
    parts += [f'- Spécialités LinkedIn : {linkedin_spec}',
              f'- Description LinkedIn (extrait) : {linkedin_desc}',
              f'- Contenu site web (extrait) : {web_snippet}']
    existing_certs = detect_existing_certs(web_snippet, linkedin_spec, linkedin_desc)
    if existing_certs:
        parts.append(f"- Note : l'entreprise semble déjà certifiée {', '.join(existing_certs)}")
    return '\n'.join(parts)


COMBINED_SYSTEM_PROMPT_EN = """You are analyzing the profile of a Quebec industrial prospect for Sonoria, a tool that automates normative non-conformity detection.

TASK 1 — DETECT SECTOR:
Sonoria automates normative compliance gap detection. A company is a TARGET if it manages documented certifications (ISO, SQF, GMP, CNESST, AS9100, IATF, etc.), regardless of sector. When in doubt, always use generique — never hors_cible.

Choose ONE code from these six:
- agroalimentaire: industrial food processing, production farm, commercial bakery, cheese factory, slaughterhouse, industrial brewery, dairy, cannery, etc. (NOT: restaurant, bar, fine grocery, caterer, café)
- manufacturing: fabrication, machining, metallurgy, welding, assembly, industrial electronics, semiconductors, mechanics, aluminum, steel, plastics, rubber, foundry, forge, tooling, industrial machinery, pharmaceutical manufacturing (GMP/BPF, ISO 13485), manufactured medical equipment, industrial automation, etc.
- construction: building, civil engineering, site work, renovation, excavation, roofing, plumbing, masonry, framing, infrastructure, general contractor, etc.
- distribution: logistics, warehousing, freight, industrial supply chain, wholesaler, 3PL, etc. (NOT: retail)
- generique: any company that could have a normative certification but whose sector is unclear or mixed — services with ISO 9001, laboratories, technical consulting, telecom, energy, environment, industrial IT, etc. When in doubt, choose generique.
- hors_cible: ONLY companies where it is CERTAIN that no normative certification exists — bar, restaurant, café, retail grocery, art gallery, law firm, real estate agency, finance firm, marketing agency. If the slightest doubt exists, use generique.

TASK 2 — IDENTIFY MOST RELEVANT NORM:
Based on sector AND specific company information, choose the most precise normative standard. If sector is hors_cible, write "N/A".

Reference by sub-sector:
- agroalimentaire: SQF (default for any Quebec company), HACCP (small company or explicit mention), FSSC 22000 (GFSI mention or large multinational), BRC/BRCGS (ONLY if explicit mention of export to European retailers)
- manufacturing general: ISO 9001
- pharmaceutical / nutraceutical / fine chemistry manufacturing: GMP
- medical device / health equipment manufacturing: ISO 13485
- aerospace / defense / aviation manufacturing: AS9100
- automotive / auto parts manufacturing: IATF 16949
- construction: CNESST
- food distribution: SQF or ISO 22000
- non-food distribution: ISO 9001
- generique: normative compliance

TASK 3 — GENERATE THE OPENER:
If sector is hors_cible, write exactly "N/A" as the opener (no other text).
Otherwise, generate a single cold email sentence using the norm from TASK 2. Structure:
"The reason I'm reaching out: as [English role] in [English sector], [concrete pain with the norm]."

PRECISION: If specific products, markets or processes are mentioned in the data, integrate them into the pain point.

ALREADY CERTIFIED: If a note indicates the company is already certified [X], adapt the angle: "maintaining your [X] certification up to date requires a manual review your team must redo every year" rather than "checking if your procedures comply".

LOCATION: If location is provided, optionally integrate it. Never invent a location.

TASK 4 — IDENTIFY SUBSECTOR:
If sector is hors_cible, agroalimentaire or generique, write "N/A".
Otherwise, provide in 2-5 words the precise subsector qualifier as it will fit in "for [SUBSECTOR] companies certified [NORM]".
Examples: "aerospace", "automotive", "pharmaceutical", "medical device", "industrial pumping", "construction".
Never write "manufacturing" if a more precise term is possible.

STRICT RULES:
- Maximum 1 sentence, entirely in English
- Never the em dash (—) or en dash (–)
- No CTA, no closing question
- Role in English: keep or translate naturally (Plant Manager, Quality Manager, President, Owner, etc.)
- Sector in English: "in the food industry", "in the manufacturing sector", "in construction", "in distribution", "in your industry"

ANGLE BY ROLE:
- Quality Director / QA Manager / VP Quality → "your team probably spends weeks each year manually verifying that your procedures comply with [X] instead of focusing on continuous improvement."
- Plant Manager / Production Manager → "the weeks leading up to a [X] audit probably pull your quality team away from production rhythm."
- President / CEO / General Manager / Owner → "a non-conformity discovered during a [X] audit costs you far more than one caught internally."
- Operations Manager / VP Operations → "your team probably spends weeks each year manually verifying that your procedures comply with [X] instead of focusing on operations."
- Engineering Manager / Technical Director → "ensuring your technical procedures meet [X] requirements demands a manual review your team must redo every year."
- Supply Chain Manager → "keeping your procedures compliant with [X] probably consumes dozens of hours of your team's time each year instead of optimizing the chain."
- Unclear title → start with "in your company, [pain]." without mentioning a role

EXAMPLES:
SECTEUR: agroalimentaire
NORME: SQF
ACCROCHE: The reason I'm reaching out: as Operations Manager in the food industry, your team probably spends weeks each year manually verifying that your procedures comply with SQF instead of focusing on production.
SOUS_SECTEUR: N/A

SECTEUR: manufacturing
NORME: AS9100
ACCROCHE: The reason I'm reaching out: as Quality Manager in the manufacturing sector, your team probably spends weeks each year manually verifying that your procedures comply with AS9100 instead of focusing on continuous improvement.
SOUS_SECTEUR: aerospace

SECTEUR: construction
NORME: CNESST
ACCROCHE: The reason I'm reaching out: as President of a construction company, a non-conformity discovered during a CNESST inspection costs you far more than one caught internally.
SOUS_SECTEUR: construction

SECTEUR: generique
NORME: normative compliance
ACCROCHE: The reason I'm reaching out: in your company, the annual normative compliance review probably consumes dozens of hours of manual verification each year instead of focusing on your core business.
SOUS_SECTEUR: N/A

SECTEUR: hors_cible
NORME: N/A
ACCROCHE: N/A
SOUS_SECTEUR: N/A

RESPOND EXACTLY IN THIS FORMAT (four lines, nothing else):
SECTEUR: [agroalimentaire|manufacturing|construction|distribution|generique|hors_cible]
NORME: [the specific norm or N/A]
ACCROCHE: [the sentence or N/A]
SOUS_SECTEUR: [the precise qualifier or N/A]"""


_VALID_SECTORS = {'agroalimentaire', 'manufacturing', 'construction', 'distribution', 'generique', 'hors_cible'}
_SONNET_SYSTEM = [{'type': 'text', 'text': COMBINED_SYSTEM_PROMPT, 'cache_control': {'type': 'ephemeral'}}]
_SONNET_SYSTEM_EN = [{'type': 'text', 'text': COMBINED_SYSTEM_PROMPT_EN, 'cache_control': {'type': 'ephemeral'}}]


def _parse_sonnet_response(text):
    secteur = 'generique'; norme = 'conformité normative'
    accroche = 'ERREUR_GENERATION'; sous_secteur = ''
    for line in text.split('\n'):
        if line.startswith('SECTEUR:'):
            val = line.split(':', 1)[1].strip().lower()
            if val in _VALID_SECTORS: secteur = val
        elif line.startswith('NORME:'):       norme        = line.split(':', 1)[1].strip()
        elif line.startswith('ACCROCHE:'):    accroche     = line.split(':', 1)[1].strip()
        elif line.startswith('SOUS_SECTEUR:'): sous_secteur = line.split(':', 1)[1].strip()
    accroche = re.sub(r' {2,}', ' ', accroche).strip()
    for ch in ['—', '–', '‒', '―']: accroche = accroche.replace(ch, '-')
    return secteur, norme, accroche, sous_secteur


def generate_sector_and_accroche(client, row, web_content=None, lang='fr'):
    user_msg = _build_user_msg(row, web_content)
    sonnet_system = _SONNET_SYSTEM_EN if lang == 'en' else _SONNET_SYSTEM
    fallback_norme = 'normative compliance' if lang == 'en' else 'conformité normative'
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=350,
                system=sonnet_system,
                messages=[{'role': 'user', 'content': user_msg}],
            )
            secteur, norme, accroche, sous_secteur = _parse_sonnet_response(resp.content[0].text.strip())
            return secteur, norme, accroche, _build_corps(secteur, norme, sous_secteur, lang=lang)
        except Exception:
            if attempt < 2: time.sleep(5)
    return 'generique', fallback_norme, 'ERREUR_GENERATION', 'ERREUR_GENERATION'


def generate_icebreaker(client, row, website_content, lang='fr'):
    company   = _s(row.get('company')) or ('the company' if lang == 'en' else "l'entreprise")
    name      = _s(row.get('name'))
    job_title = _s(row.get('job_title')) or _s(row.get('result_title'))
    headline  = _s(row.get('headline'))
    job_desc  = _s(row.get('job_description'))
    linkedin_desc = _s(row.get('linkedin_description'))
    linkedin_spec = _s(row.get('linkedin_specialities'))
    summary  = _s(row.get('summary'))
    location = _s(row.get('location'))

    if lang == 'en':
        context_parts = [f"Company: {company}"]
        if name:        context_parts.append(f"Contact: {name}" + (f" ({job_title})" if job_title else ''))
        if headline:    context_parts.append(f"LinkedIn Headline: {headline}")
        if location:    context_parts.append(f"Location: {location}")
        if linkedin_desc: context_parts.append(f"LinkedIn Description: {linkedin_desc[:500]}")
        if linkedin_spec: context_parts.append(f"LinkedIn Specialties: {linkedin_spec[:300]}")
        if job_desc:    context_parts.append(f"Job Description: {job_desc[:300]}")
        if summary:     context_parts.append(f"Contact Summary: {summary[:300]}")
        if website_content:
            context_parts.append(f"\nWebsite content:\n{website_content[:800]}")
        else:
            context_parts.append("\nNote: no website content available. You must still generate the icebreaker using the LinkedIn information above. Never mention that the site is not accessible.")
        user_msg = '\n'.join(context_parts) + f'\n\nGenerate the icebreaker for {company}.'
    else:
        context_parts = [f"Entreprise: {company}"]
        if name:        context_parts.append(f"Contact: {name}" + (f" ({job_title})" if job_title else ''))
        if headline:    context_parts.append(f"Headline LinkedIn: {headline}")
        if location:    context_parts.append(f"Localisation: {location}")
        if linkedin_desc: context_parts.append(f"Description LinkedIn: {linkedin_desc[:500]}")
        if linkedin_spec: context_parts.append(f"Spécialités LinkedIn: {linkedin_spec[:300]}")
        if job_desc:    context_parts.append(f"Description du poste: {job_desc[:300]}")
        if summary:     context_parts.append(f"Résumé du contact: {summary[:300]}")
        if website_content:
            context_parts.append(f"\nContenu du site web:\n{website_content[:800]}")
        else:
            context_parts.append("\nNote: aucun contenu de site web disponible. Tu dois quand même générer l'icebreaker en utilisant les informations LinkedIn ci-dessus. Ne mentionne jamais que le site n'est pas accessible.")
        user_msg = '\n'.join(context_parts) + f"\n\nGénère l'icebreaker pour {company}."

    sys_prompt = SYSTEM_PROMPT_EN if lang == 'en' else SYSTEM_PROMPT
    response = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=200,
        system=[{'type': 'text', 'text': sys_prompt, 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': user_msg}],
    )
    text = response.content[0].text.strip()
    for ch in ['—', '–', '‒', '―']:
        text = text.replace(ch, ' ')
    return re.sub(r' {2,}', ' ', text).strip()


# ---------------------------------------------------------------------------
# Background icebreaker job
# ---------------------------------------------------------------------------

def _push_event(job_id, event_type, data):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]['events'].append({'type': event_type, 'data': data})


_SCRAPE_WORKERS = 20
_API_WORKERS    = 15


def _scrape_all_parallel(df):
    """Scrape all prospect websites concurrently. Returns list[web_content|None]."""
    rows = list(df.iterrows())
    results = [None] * len(rows)

    def scrape_one(args):
        idx, (_, row) = args
        website = _s(row.get('corporate_website')) or _s(row.get('domain_name'))
        if website and not _linkedin_data_sufficient(row):
            return idx, scrape_website(website)
        return idx, None

    workers = min(_SCRAPE_WORKERS, len(rows)) if rows else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(scrape_one, item): item[0] for item in enumerate(rows)}
        for fut in as_completed(futures):
            try:
                idx, content = fut.result()
                results[idx] = content
            except Exception:
                pass
    return results


def _generate_icebreakers_parallel(client, df_rows, secteurs, web_contents, lang='fr'):
    """Generate all icebreakers concurrently. Returns list[str]."""
    total = len(df_rows)
    results = [''] * total

    def gen_one(i):
        _, row = df_rows[i]
        if secteurs[i] == 'hors_cible':
            return i, ''
        try:
            return i, generate_icebreaker(client, row, web_contents[i], lang=lang)
        except Exception as e:
            return i, f'[Erreur: {str(e)[:80]}]'

    workers = min(_API_WORKERS, total) if total else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(gen_one, i): i for i in range(total)}
        for fut in as_completed(futures):
            try:
                i, ice = fut.result()
                results[i] = ice
            except Exception:
                pass
    return results


def run_icebreaker_job(job_id, df, api_key, lang='fr'):
    client = anthropic.Anthropic(api_key=api_key)
    df = df[~df['job_title'].apply(lambda t: is_bad_role(_s(t)))].reset_index(drop=True)
    total = len(df)
    icebreakers = [''] * total
    accroches_conformite = [''] * total
    corps_emails = [''] * total
    secteurs = [''] * total
    normes = [''] * total

    # Pré-scraping parallèle — tous les sites en même temps
    _push_event(job_id, 'progress', {'i': 0, 'total': total, 'company': '', 'status': 'scraping'})
    web_contents = _scrape_all_parallel(df)

    for i, (_, row) in enumerate(df.iterrows()):
        company = _s(row.get('company')) or _s(row.get('name')) or f'prospect {i+1}'
        web_content = web_contents[i]

        _push_event(job_id, 'progress', {
            'i': i, 'total': total, 'company': company,
            'status': 'classifying', 'scraped': bool(web_content)
        })
        secteur, norme, accroche_conformite, corps_email = generate_sector_and_accroche(client, row, web_content, lang=lang)

        _push_event(job_id, 'progress', {
            'i': i, 'total': total, 'company': company,
            'status': 'generating', 'secteur': secteur
        })
        if secteur == 'hors_cible':
            icebreaker = ''
        else:
            try:
                icebreaker = generate_icebreaker(client, row, web_content, lang=lang)
            except Exception as e:
                icebreaker = f'[Erreur: {str(e)[:80]}]'

        icebreakers[i] = icebreaker
        accroches_conformite[i] = accroche_conformite
        corps_emails[i] = corps_email
        secteurs[i] = secteur
        normes[i] = norme
        _push_event(job_id, 'result', {
            'i': i, 'company': company, 'icebreaker': icebreaker,
            'accroche_conformite': accroche_conformite, 'secteur': secteur,
            'norme': norme, 'corps_email': corps_email,
        })

    final_total, excluded = _save_job_csv(job_id, df, secteurs, normes, accroches_conformite, corps_emails, icebreakers)
    with _jobs_lock:
        _jobs[job_id]['done'] = True
    _push_event(job_id, 'done', {'total': final_total, 'excluded': excluded})


def _save_job_csv(job_id, df, secteurs, normes, accroches, corps_list, icebreakers):
    df = df.copy()
    for col in ('icebreaker', 'accroche', 'accroche_conformite'):
        if col in df.columns: df = df.drop(columns=[col])
    df.insert(0, 'secteur_detecte',     secteurs)
    df.insert(0, 'norme_detectee',      normes)
    df.insert(0, 'corps_email',         corps_list)
    df.insert(0, 'accroche_conformite', accroches)
    df.insert(0, 'icebreaker',          icebreakers)
    excluded = int((df['secteur_detecte'] == 'hors_cible').sum())
    df = df[df['secteur_detecte'] != 'hors_cible'].reset_index(drop=True)
    df.to_csv(os.path.join(RESULTS_DIR, f'{job_id}.csv'), index=False, encoding='utf-8-sig')
    return len(df), excluded


def _save_procedures_csv(job_id, df, icebreakers, accroches_proc):
    df = df.copy()
    for col in ('icebreaker', 'accroche', 'accroche_procedures'):
        if col in df.columns: df = df.drop(columns=[col])
    df.insert(0, 'accroche_procedures', accroches_proc)
    df.insert(0, 'icebreaker',          icebreakers)
    df.to_csv(os.path.join(RESULTS_DIR, f'{job_id}.csv'), index=False, encoding='utf-8-sig')
    return len(df)


def run_icebreaker_job_batch(job_id, df, api_key, lang='fr'):
    client = anthropic.Anthropic(api_key=api_key)
    df = df[~df['job_title'].apply(lambda t: is_bad_role(_s(t)))].reset_index(drop=True)
    total = len(df)

    # Phase 1 — scraping parallèle de tous les sites
    _push_event(job_id, 'progress', {'i': 0, 'total': total, 'company': '', 'status': 'scraping'})
    web_contents = _scrape_all_parallel(df)

    # Phase 2 — submit Sonnet batch (1 request per prospect)
    _push_event(job_id, 'progress', {'i': 0, 'total': total, 'company': '', 'status': 'batch_submitting'})
    batch_requests = []
    for i, (_, row) in enumerate(df.iterrows()):
        batch_requests.append({
            'custom_id': str(i),
            'params': {
                'model': 'claude-sonnet-4-6',
                'max_tokens': 350,
                'system': _SONNET_SYSTEM_EN if lang == 'en' else _SONNET_SYSTEM,
                'messages': [{'role': 'user', 'content': _build_user_msg(row, web_contents[i])}],
            },
        })
    batch = client.beta.messages.batches.create(requests=batch_requests)
    batch_id = batch.id

    # Phase 3 — poll until Anthropic finishes processing
    while True:
        time.sleep(30)
        batch = client.beta.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        done_count = counts.succeeded + counts.errored + counts.canceled + counts.expired
        _push_event(job_id, 'progress', {
            'i': done_count, 'total': total, 'company': '',
            'status': 'batch_waiting', 'batch_done': done_count,
        })
        if batch.processing_status == 'ended':
            break

    # Phase 4 — collect Sonnet results
    secteurs  = ['generique'] * total
    normes    = ['conformité normative'] * total
    accroches = ['ERREUR_GENERATION'] * total
    corps_list = ['ERREUR_GENERATION'] * total
    for result in client.beta.messages.batches.results(batch_id):
        i = int(result.custom_id)
        if result.result.type == 'succeeded':
            secteur, norme, accroche, sous_secteur = _parse_sonnet_response(
                result.result.message.content[0].text.strip()
            )
            corps = _build_corps(secteur, norme, sous_secteur, lang=lang)
        else:
            secteur, norme, accroche, corps = 'generique', 'conformité normative', 'ERREUR_GENERATION', 'ERREUR_GENERATION'
        secteurs[i]   = secteur
        normes[i]     = norme
        accroches[i]  = accroche
        corps_list[i] = corps

    # Phase 5 — icebreakers Haiku en parallèle + accroches procédures (rule-based)
    _push_event(job_id, 'progress', {'i': 0, 'total': total, 'company': '', 'status': 'generating'})
    df_rows = list(df.iterrows())
    icebreakers = _generate_icebreakers_parallel(client, df_rows, secteurs, web_contents, lang=lang)

    accroches_proc = [
        '' if secteurs[i] == 'hors_cible' else _build_accroche_procedures(
            _s(row.get('job_title')) or _s(row.get('result_title')),
            _s(row.get('first_name')),
        )
        for i, (_, row) in enumerate(df.iterrows())
    ]

    for i, (_, row) in enumerate(df.iterrows()):
        company = _s(row.get('company')) or _s(row.get('name')) or f'prospect {i+1}'
        _push_event(job_id, 'result', {
            'i': i, 'company': company, 'icebreaker': icebreakers[i],
            'accroche_procedures': accroches_proc[i],
            'accroche_conformite': accroches[i], 'secteur': secteurs[i],
            'norme': normes[i], 'corps_email': corps_list[i],
        })

    final_total, excluded = _save_job_csv(job_id, df, secteurs, normes, accroches, corps_list, icebreakers)
    with _jobs_lock:
        _jobs[job_id]['done'] = True
    _push_event(job_id, 'done', {'total': final_total, 'excluded': excluded})


def run_icebreaker_job_procedures(job_id, df, api_key, lang='fr'):
    client = anthropic.Anthropic(api_key=api_key)
    df = df[~df['job_title'].apply(lambda t: is_bad_role(_s(t)))].reset_index(drop=True)
    total = len(df)
    df_rows = list(df.iterrows())
    icebreakers = [''] * total

    # Accroches rule-based : instant, on les calcule d'abord
    accroches_proc = [
        _build_accroche_procedures(
            _s(row.get('job_title')) or _s(row.get('result_title')),
            _s(row.get('first_name')),
            lang=lang,
        )
        for _, row in df.iterrows()
    ]

    _push_event(job_id, 'progress', {'i': 0, 'total': total, 'company': '', 'status': 'scraping'})

    def scrape_one(i):
        _, row = df_rows[i]
        website = _s(row.get('corporate_website')) or _s(row.get('domain_name'))
        if website and not _linkedin_data_sufficient(row):
            return i, scrape_website(website)
        return i, None

    def gen_ice(i, web_content):
        _, row = df_rows[i]
        try:
            return i, generate_icebreaker(client, row, web_content, lang)
        except Exception as e:
            return i, f'[Erreur: {str(e)[:80]}]'

    # Pipeline : dès qu'un scrape est terminé, on lance le Haiku immédiatement
    # Temps total ≈ max(scraping, haiku) au lieu de scraping + haiku
    workers = min(_SCRAPE_WORKERS + _API_WORKERS, max(total, 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        scrape_futs = {ex.submit(scrape_one, i): i for i in range(total)}
        haiku_futs = {}
        for fut in as_completed(scrape_futs):
            try:
                i, web_content = fut.result()
            except Exception:
                i = scrape_futs[fut]
                web_content = None
            haiku_futs[ex.submit(gen_ice, i, web_content)] = i

        for fut in as_completed(haiku_futs):
            try:
                i, ice = fut.result()
            except Exception:
                i = haiku_futs[fut]
                ice = ''
            icebreakers[i] = ice
            _, row = df_rows[i]
            company = _s(row.get('company')) or _s(row.get('name')) or f'prospect {i+1}'
            _push_event(job_id, 'result', {
                'i': i, 'company': company,
                'icebreaker': ice,
                'accroche_procedures': accroches_proc[i],
            })

    final_total = _save_procedures_csv(job_id, df, icebreakers, accroches_proc)
    with _jobs_lock:
        _jobs[job_id]['done'] = True
    _push_event(job_id, 'done', {'total': final_total, 'excluded': 0})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/combine', methods=['POST'])
def combine():
    files = request.files.getlist('files')
    if len(files) < 2:
        return jsonify({'error': 'Minimum 2 fichiers requis.'}), 400
    filter_db = request.form.get('filter_db') == 'true'
    try:
        combined, file_stats, before, after = combine_files(files)
        db_removed = 0
        if filter_db:
            filtered = db_filter_df(combined)
            db_removed = len(combined) - len(filtered)
            combined = filtered
        output = io.BytesIO()
        combined.to_csv(output, index=False, encoding='utf-8-sig')
        output.seek(0)
        return jsonify({
            'stats': {
                'files': file_stats,
                'total_before': before,
                'duplicates_removed': before - after,
                'db_removed': db_removed,
                'total_after': len(combined),
            },
            'csv': output.getvalue().decode('utf-8-sig'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/db/stats')
def db_stats_route():
    return jsonify(db_stats())


@app.route('/db/add', methods=['POST'])
def db_add_route():
    files = request.files.getlist('files')
    table = request.form.get('table', 'contacted_leads')
    list_name = request.form.get('list_name', '').strip() or 'Sans nom'
    if table not in ('contacted_leads', 'blacklist'):
        return jsonify({'error': 'Table invalide.'}), 400
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'Aucun fichier fourni.'}), 400
    now = datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('INSERT INTO lists (name, table_name, created_date) VALUES (?, ?, ?)',
                           (list_name, table, now))
        list_id = cur.lastrowid
        conn.commit()
    total_added, total_skipped = 0, 0
    for f in files:
        try:
            df = read_file(f)
            col_map = map_columns(df)
            df = df.rename(columns=col_map)
            for col in ['linkedin_url', 'email', 'name', 'company']:
                if col not in df.columns:
                    df[col] = ''
            added, skipped = db_add_df(df, table=table, list_id=list_id)
            total_added += added
            total_skipped += skipped
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    # Remove list entry if nothing was actually added
    if total_added == 0:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('DELETE FROM lists WHERE id = ?', (list_id,))
            conn.commit()
        list_id = None
    stats = db_stats()
    key = 'contacted_total' if table == 'contacted_leads' else 'blacklist_total'
    last_key = 'contacted_last' if table == 'contacted_leads' else 'blacklist_last'
    return jsonify({
        'added': total_added,
        'skipped': total_skipped,
        'db_total': stats[key],
        'last_added': stats[last_key],
        'list_id': list_id,
    })


@app.route('/db/lists')
def db_lists_route():
    table = request.args.get('table', 'contacted_leads')
    if table not in ('contacted_leads', 'blacklist'):
        return jsonify({'error': 'Table invalide.'}), 400
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(f'''
            SELECT l.id, l.name, l.created_date, COUNT(t.id) as cnt
            FROM lists l
            LEFT JOIN {table} t ON t.list_id = l.id
            WHERE l.table_name = ?
            GROUP BY l.id
            ORDER BY l.created_date DESC
        ''', (table,)).fetchall()
    return jsonify([{'id': r[0], 'name': r[1], 'date': r[2][:10], 'count': r[3]} for r in rows])


@app.route('/db/lists/<int:list_id>/rename', methods=['POST'])
def db_list_rename(list_id):
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Nom requis.'}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('UPDATE lists SET name = ? WHERE id = ?', (name, list_id))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/db/lists/<int:list_id>/download')
def db_list_download(list_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT name, table_name FROM lists WHERE id = ?', (list_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Liste introuvable.'}), 404
        list_name, tbl = row
        rows = conn.execute(
            f'SELECT name, company, email, linkedin_url FROM {tbl} WHERE list_id = ?', (list_id,)
        ).fetchall()
    df = pd.DataFrame(rows, columns=['name', 'company', 'email', 'linkedin_url'])
    output = io.BytesIO()
    df.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    safe = re.sub(r'[^\w\s-]', '', list_name).strip().replace(' ', '_') or 'liste'
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name=f'{safe}.csv')


@app.route('/db/lists/<int:list_id>/delete', methods=['POST'])
def db_list_delete(list_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT table_name FROM lists WHERE id = ?', (list_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Liste introuvable.'}), 404
        tbl = row[0]
        conn.execute(f'DELETE FROM {tbl} WHERE list_id = ?', (list_id,))
        conn.execute('DELETE FROM lists WHERE id = ?', (list_id,))
        conn.commit()
    return jsonify({'ok': True, **db_stats()})


@app.route('/db/filter', methods=['POST'])
def db_filter_route():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'Fichier requis.'}), 400
    try:
        df_orig = read_file(file)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    col_map = map_columns(df_orig)
    df_mapped = df_orig.rename(columns=col_map)
    for col in ['linkedin_url', 'email']:
        if col not in df_mapped.columns:
            df_mapped[col] = ''
    before = len(df_orig)
    c_li, c_em, b_li, b_em = _db_get_known_sets()
    li_col = df_mapped['linkedin_url']
    em_col = df_mapped['email']

    def is_excluded(i):
        li = _s(li_col.iloc[i])
        em = _s(em_col.iloc[i])
        return (_is_in_sets(li, em, c_li, c_em) or _is_in_sets(li, em, b_li, b_em))

    mask = pd.Series([bool(is_excluded(i)) for i in range(before)])
    df_result = df_orig[~mask.values].reset_index(drop=True)
    after = len(df_result)
    output = io.BytesIO()
    df_result.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    return jsonify({
        'before': before,
        'removed': before - after,
        'after': after,
        'csv': output.getvalue().decode('utf-8-sig'),
    })


@app.route('/icebreaker/start', methods=['POST'])
def icebreaker_start():
    try:
        return _icebreaker_start_inner()
    except Exception as e:
        return jsonify({'error': f'Erreur serveur: {str(e)}'}), 500


def _icebreaker_start_inner():
    api_key = request.form.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'Clé API Anthropic requise.'}), 400

    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'Fichier requis.'}), 400

    try:
        df = read_file(file)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    # Normalize columns best-effort
    col_map = map_columns(df)
    df = df.rename(columns=col_map)
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ''

    limit = request.form.get('limit', '').strip()
    if limit.isdigit() and int(limit) > 0:
        df = df.head(int(limit))

    mode = request.form.get('mode', 'realtime')
    icebreaker_type = request.form.get('icebreaker_type', 'conformites')
    lang = request.form.get('lang', 'fr')
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'events': [], 'done': False}

    if icebreaker_type == 'procedures':
        target = run_icebreaker_job_procedures
    else:
        target = run_icebreaker_job_batch if mode == 'batch' else run_icebreaker_job
    thread = threading.Thread(target=target, args=(job_id, df, api_key, lang), daemon=True)
    thread.start()

    return jsonify({'job_id': job_id, 'total': len(df), 'mode': mode})


@app.route('/icebreaker/stream/<job_id>')
def icebreaker_stream(job_id):
    def generate():
        cursor = 0
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'type': 'error', 'data': 'Job introuvable'})}\n\n"
                    return
                events = job['events'][cursor:]
                done = job['done']

            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
            cursor += len(events)

            if done and not events:
                return
            if done:
                return

            time.sleep(0.3)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/icebreaker/download/<job_id>')
def icebreaker_download(job_id):
    result_path = os.path.join(RESULTS_DIR, f'{job_id}.csv')
    if not os.path.exists(result_path):
        return jsonify({'error': 'Résultat non disponible.'}), 404
    return send_file(
        result_path,
        mimetype='text/csv',
        as_attachment=True,
        download_name='prospects_icebreakers.csv',
    )


# ---------------------------------------------------------------------------
# Industry filter — classify leads and remove obvious off-sector targets
# ---------------------------------------------------------------------------

_INDUSTRY_FILTER_PROMPT = """Tu analyses des entreprises pour Sonoria afin de déterminer si elles font partie des secteurs industriels cibles.

SECTEURS CIBLES : manufacturier, distribution/logistique, agroalimentaire (transformation industrielle), construction.

RÈGLE ABSOLUE — ULTRA CONSERVATEUR : Vaut bien mieux garder une mauvaise cible qu'exclure une bonne. N'utilise hors_cible QUE si tu es CERTAIN à plus de 95% que l'entreprise n'a strictement aucun rapport avec le monde industriel.

Codes disponibles :
- manufacturier : fabrication, usinage, assemblage, métallurgie, électronique industrielle, pharmaceutique, dispositifs médicaux, aérospatial, automobile, automatisation industrielle, transformation industrielle, impression industrielle, équipements industriels
- distribution : logistique, entreposage, transport de marchandises, grossiste, 3PL, supply chain, fret
- agroalimentaire : transformation alimentaire industrielle, ferme de production, brasserie industrielle, fromagerie, abattoir, laiterie, conserverie
- construction : bâtiment, génie civil, rénovation commerciale/industrielle, excavation, entrepreneur général, toiture commerciale, infrastructure
- autre_cible : toute entreprise industrielle ou de services à l'industrie avec secteur flou — services B2B industriels, TI industriel, génie-conseil technique, énergie, environnement, recyclage, automatisation, maintenance industrielle, laboratoires, télécommunications industrielles. EN CAS DE DOUTE → autre_cible.
- hors_cible : UNIQUEMENT si CERTAIN — restaurant, bar, café, épicerie de détail, salon de coiffure, galerie d'art, cabinet d'avocats, agence immobilière résidentielle, firme de finance/investissement, agence marketing/pub pure, école primaire/secondaire, artiste, influenceur, médecin en pratique privée. Si le moindre doute existe → autre_cible.

RÉPONDS EXACTEMENT en 2 lignes, rien d'autre :
SECTEUR: [manufacturier|distribution|agroalimentaire|construction|autre_cible|hors_cible]
RAISON: [5 à 10 mots expliquant la décision]"""

_FILTER_SYSTEM = [{'type': 'text', 'text': _INDUSTRY_FILTER_PROMPT, 'cache_control': {'type': 'ephemeral'}}]

_FILTER_VALID = {'manufacturier', 'distribution', 'agroalimentaire', 'construction', 'autre_cible', 'hors_cible'}


def _build_filter_msg(row):
    company       = _s(row.get('company'))
    headline      = _s(row.get('headline'))
    job_title     = _s(row.get('job_title'))
    linkedin_desc = _s(row.get('linkedin_description'))[:400]
    linkedin_spec = _s(row.get('linkedin_specialities'))[:200]
    summary       = _s(row.get('summary'))[:200]
    job_desc      = _s(row.get('job_description'))[:200]

    parts = [f'Entreprise: {company}']
    if headline:      parts.append(f'Headline: {headline}')
    if job_title:     parts.append(f'Titre du contact: {job_title}')
    if linkedin_desc: parts.append(f'Description LinkedIn: {linkedin_desc}')
    if linkedin_spec: parts.append(f'Spécialités LinkedIn: {linkedin_spec}')
    if summary:       parts.append(f'Résumé du contact: {summary}')
    if job_desc:      parts.append(f'Description du poste: {job_desc}')
    return '\n'.join(parts)


def _classify_one(client, row):
    msg = _build_filter_msg(row)
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=60,
                system=_FILTER_SYSTEM,
                messages=[{'role': 'user', 'content': msg}],
            )
            text = resp.content[0].text.strip()
            secteur, raison = 'autre_cible', ''
            for line in text.split('\n'):
                if line.startswith('SECTEUR:'):
                    val = line.split(':', 1)[1].strip().lower()
                    if val in _FILTER_VALID:
                        secteur = val
                elif line.startswith('RAISON:'):
                    raison = line.split(':', 1)[1].strip()
            return secteur, raison
        except Exception:
            if attempt < 2:
                time.sleep(3)
    return 'autre_cible', 'erreur classification'


def run_industry_filter_job(job_id, df, api_key):
    try:
        client = anthropic.Anthropic(api_key=api_key)
        total = len(df)
        df_rows = list(df.iterrows())
        secteurs = ['autre_cible'] * total
        raisons  = [''] * total
        done_count = 0

        _push_event(job_id, 'progress', {'done': 0, 'total': total, 'status': 'classifying'})

        def classify_one_indexed(i):
            _, row = df_rows[i]
            s, r = _classify_one(client, row)
            return i, s, r

        workers = min(25, total) if total else 1
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(classify_one_indexed, i): i for i in range(total)}
            for fut in as_completed(futures):
                try:
                    i, s, r = fut.result()
                except Exception:
                    i = futures[fut]
                    s, r = 'autre_cible', 'erreur'
                secteurs[i] = s
                raisons[i]  = r
                done_count += 1
                _push_event(job_id, 'progress', {'done': done_count, 'total': total, 'status': 'classifying'})

        result_df = df.copy()
        result_df.insert(0, 'raison_filtre',  raisons)
        result_df.insert(0, 'secteur_filtre', secteurs)
        excluded = int(sum(1 for s in secteurs if s == 'hors_cible'))
        kept_df  = result_df[result_df['secteur_filtre'] != 'hors_cible'].reset_index(drop=True)
        kept_df.to_csv(os.path.join(RESULTS_DIR, f'filter_{job_id}.csv'), index=False, encoding='utf-8-sig')

        with _jobs_lock:
            _jobs[job_id]['done'] = True
        _push_event(job_id, 'done', {'kept': len(kept_df), 'excluded': excluded, 'total': total})

    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]['done'] = True
        _push_event(job_id, 'error', str(e))


@app.route('/industry/filter/start', methods=['POST'])
def industry_filter_start():
    api_key = request.form.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'Clé API Anthropic requise.'}), 400
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'Fichier requis.'}), 400
    try:
        df = read_file(file)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    col_map = map_columns(df)
    df = df.rename(columns=col_map)
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ''

    limit = request.form.get('limit', '').strip()
    if limit.isdigit() and int(limit) > 0:
        df = df.head(int(limit))

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'events': [], 'done': False}

    thread = threading.Thread(target=run_industry_filter_job, args=(job_id, df, api_key), daemon=True)
    thread.start()
    return jsonify({'job_id': job_id, 'total': len(df)})


@app.route('/industry/filter/stream/<job_id>')
def industry_filter_stream(job_id):
    def generate():
        cursor = 0
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'type': 'error', 'data': 'Job introuvable'})}\n\n"
                    return
                events = job['events'][cursor:]
                done = job['done']
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
            cursor += len(events)
            if done and not events:
                return
            if done:
                return
            time.sleep(0.3)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/industry/filter/download/<job_id>')
def industry_filter_download(job_id):
    result_path = os.path.join(RESULTS_DIR, f'filter_{job_id}.csv')
    if not os.path.exists(result_path):
        return jsonify({'error': 'Résultat non disponible.'}), 404
    return send_file(result_path, mimetype='text/csv', as_attachment=True,
                     download_name='leads_filtres_secteur.csv')


@app.route('/deploy', methods=['POST'])
def deploy_webhook():
    payload = request.get_data()
    signature = request.headers.get('X-Hub-Signature-256', '')
    expected = 'sha256=' + hmac.new(DEPLOY_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify({'error': 'Unauthorized'}), 401

    def _run():
        time.sleep(3)
        subprocess.Popen(
            ['cmd', '/c', os.path.join(_BASE_DIR, 'deploy.bat')],
            cwd=_BASE_DIR,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)
