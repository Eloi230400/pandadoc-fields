"""Micro-service "Contrat PandaDoc" — Arthaud Immobilier Académie
=================================================================
Assemble un contrat PandaDoc en UN SEUL document, en 3 morceaux :
  1) le CORPS du contrat = le PDF PDFMonkey (pages AVANT la page signature)
  2) la PAGE SIGNATURE   = un MODELE PandaDoc natif (2 cases OBLIGATOIRES + signature)
  3) les ANNEXES         = les pages APRES la page signature (ajoutées en dernier)

Pourquoi un modele natif : PandaDoc ne permet pas de rendre une case obligatoire
sur un PDF importe. La seule facon d'avoir des cases obligatoires en automatique =
les mettre dans un MODELE PandaDoc (configure une fois dans l'editeur).

>>> ASYNCHRONE <<<
Le montage complet cote PandaDoc prend ~70-80 s (traitement async des sections).
C'est trop long pour une etape Zapier (coupure ~30 s). Donc :
  - on cree le document (rapide, ~5-10 s) et on renvoie TOUT DE SUITE document_id + edit_url
  - l'ajout des sections (page signature + annexes) se termine EN TACHE DE FOND.
Zapier recoit une reponse en ~10 s ; le brouillon finit de s'assembler seul.

ENV requis : PANDADOC_API_KEY
ENV option : AIRTABLE_TOKEN (base Commercial & Compta) — v22 : permet de pre-cocher les
             cases de la page signature d'apres la vente (voir plus bas).
             v24 : le jeton doit aussi pouvoir ECRIRE (scope data.records:write) pour
             renvoyer dans la vente l'ID du document, le lien de signature du client et
             le statut reel de l'envoi. Verifier avec GET /airtable-check.
Endpoint   : POST /create-draft
  Body JSON : {
    "pdf_url":            "<URL du PDF PDFMonkey complet>",
    "contract_type":      "b2b"  ou  "b2c",
    "client_email":       "...",
    "client_first_name":  "...",
    "client_last_name":   "...",
    "document_name":      "Contrat Mastermind ..."   (optionnel)
  }
  -> renvoie {ok, document_id, edit_url} immediatement (brouillon en cours de montage).
Endpoint   : GET /status/<document_id>  -> etat courant + suivi du montage en fond.
"""
import os, io, gc, json, time, threading, traceback, requests, fitz
from flask import Flask, request, jsonify

app = Flask(__name__)
PANDADOC = "https://api.pandadoc.com/public/v1"

# Modeles PandaDoc "page signature" (crees dans l'editeur).
# 01/09/2026 : CONSOLIDATION en 3 modeles (page propre 3 cases -> 1-2 facultatives,
# 3 obligatoire ; B2B = 2 cases dont "j'ai lu et j'accepte" obligatoire). Role "Role 1".
#   - B2C accompagnement (incubateur/mastermind/elite) : XuztudELSdVshWxWtozxK4
#   - B2C formation (formation/starter)                : AcoiwPniXp6vJkJdwXoNDi
#   - B2B (tous programmes)                            : SqyRSvuQ9yEXXF66x9shXL
# Ancienne table (rollback) : b2b=u8jMx9jGwNXFXUK4JDH8uN b2c=9F7HwBwA7QUjhWCUUAn8aj
#   b2b:incubateur=EZgpsbDUidtEEpBTQJaQxL b2c:incubateur=r85DmX64qKQfFTTtZvsuq9
#   b2b:formation=spoAWUWuysMp4oKckbu6LC b2c:formation=MKK6TUSLdmjShEe6tL2CjC
#   b2b:starter=iREzxvQhESHQ6DTtgKTceF b2c:starter=7NDpbrqUrSNEnSywJgQxWd
#   b2b:elite=mcPnAf4rDy9KYvADAjGCNX b2c:elite=j3KQxcwb2u7ynDKtYfCik2
TEMPLATES = {
    "b2b": "SqyRSvuQ9yEXXF66x9shXL",              # B2B consolide (Mastermind)
    "b2c": "XuztudELSdVshWxWtozxK4",              # B2C accompagnement (Mastermind)
    "b2b:incubateur": "SqyRSvuQ9yEXXF66x9shXL",   # B2B consolide
    "b2c:incubateur": "XuztudELSdVshWxWtozxK4",   # B2C accompagnement
    "b2b:formation": "SqyRSvuQ9yEXXF66x9shXL",    # B2B consolide
    "b2c:formation": "AcoiwPniXp6vJkJdwXoNDi",    # B2C formation
    "b2b:starter": "SqyRSvuQ9yEXXF66x9shXL",      # B2B consolide
    "b2c:starter": "AcoiwPniXp6vJkJdwXoNDi",      # B2C formation (starter)
    "b2b:elite": "SqyRSvuQ9yEXXF66x9shXL",        # B2B consolide
    "b2c:elite": "XuztudELSdVshWxWtozxK4",        # B2C accompagnement (elite)
}

# libelle du programme utilise dans le corps de l'email d'envoi
PROGRAMMES = {
    "incubateur": "l'Incubateur",
    "starter": "la formation Starter",
    "formation": "la Formation",
    "elite": "le Mastermind Elite",
    "": "le Mastermind",
}


def detect_prod_key(produit_brut: str) -> str:
    """Deduit la famille de produit a partir du libelle Airtable "Produit".
    L'ordre compte : "Formation Starter" doit tomber sur "starter", pas sur
    "formation". "" = Mastermind (comportement historique inchange)."""
    p = (produit_brut or "").lower()
    if "elite" in p:               # "Mastermind Elite" -> AVANT "mastermind"
        return "elite"
    if "incubateur" in p:
        return "incubateur"
    if "mastermind" in p:
        return ""
    if "starter" in p:
        return "starter"
    if "formation" in p or "classique" in p:
        return "formation"
    return ""
TEMPLATE_ROLE = "Role 1"            # role defini dans les modeles
SIG_TAG = "[signature:client:sig]"  # sert a reperer/retirer la page signature du corps

# v22 (08/09/2026) — CASES PRE-RENSEIGNEES D'APRES LA VISIO.
# Le closer recueille en visio (enregistree) l'accord oral du client sur
# l'acces immediat et la renonciation "contenus numeriques", et le note dans le
# formulaire "Envoyer un contrat" -> table Ventes. Le service relit la vente
# (record_id) et PRE-COCHE les cases du modele signature. Les cases restent
# assignees au signataire : le client peut les decocher avant de signer (c'est
# ce qui garde le consentement "expres"). La case "j'ai lu et j'accepte" est
# toujours pre-cochee (decision Eloi 08/09). Sans jeton Airtable ou en cas
# d'erreur => aucune case pre-cochee (comportement v21), jamais de blocage.
AIRTABLE_BASE = "app6xDX8P4RXOpNHB"
AIRTABLE_VENTES = "tbllDmX5Oyb5mJilB"
FIELD_ACCES = "Accès immédiat demandé (visio)"
FIELD_RENONCIATION = "Renonciation rétractation numérique (visio)"
# noms de champ de fusion (Field ID) des cases dans les 3 modeles PandaDoc
MERGE_ACCES = "acces"
MERGE_RENONCIATION = "renonciation"   # modeles B2C uniquement
MERGE_CONDITIONS = "conditions"
# v23 — cases toujours cochees (decision Eloi 09/09). Mettre FORCE_CHECKBOXES=0
# dans l'environnement Render pour revenir au comportement v22 (lecture Airtable).
FORCE_CHECKBOXES = os.environ.get("FORCE_CHECKBOXES", "1").strip().lower() not in ("0", "false", "non", "no")


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "oui", "yes", "on", "x")


def fetch_visio_choices(record_id: str):
    """Relit la vente Airtable et renvoie {"acces": bool, "renonciation": bool},
    ou None si indisponible (pas de jeton, erreur reseau, champ absent...)."""
    token = os.environ.get("AIRTABLE_TOKEN")
    if not token or not record_id:
        return None
    try:
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_VENTES}/{record_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code >= 400:
            return None
        f = r.json().get("fields", {})
        acces = _truthy(f.get(FIELD_ACCES))
        renonc = _truthy(f.get(FIELD_RENONCIATION)) and acces  # pas de renonciation sans acces immediat
        return {"acces": acces, "renonciation": renonc}
    except Exception:
        return None

# v24 (15/09/2026) — RETOUR DANS AIRTABLE APRES L'ENVOI REEL.
# Jusqu'ici le Zap posait "Statut contrat = Envoyé" des que le service acceptait
# la demande, AVANT le montage et l'envoi PandaDoc (qui durent ~60-90 s en tache
# de fond). Si le montage mourait en route (ex. Corbanini V-2026-0597 le 15/09 :
# brouillon jamais envoye), Airtable affichait quand meme "Envoyé" et personne
# ne le voyait. Et l'ID du document PandaDoc n'etait stocke nulle part : la
# seule cle etait la metadata record_id cote PandaDoc.
# Desormais le service ecrit lui-meme dans la vente (record_id) :
#   - des la creation du document : _PandaDoc ID (permet de retrouver un
#     brouillon orphelin) ;
#   - apres l'envoi reel : le lien de signature personnel du client
#     (recipients[].shared_link du document, le meme que "Partager via un lien"
#     dans PandaDoc), dans un champ RESERVE A LA DIRECTION (jamais affiche aux
#     closers/setters : la Direction le transmet au closer, qui l'envoie au
#     client, et seul le client agit), + Statut contrat = "Envoyé" (confirme) ;
#   - en cas d'echec du montage/envoi : Statut contrat = "⚠️ Erreur envoi" +
#     le detail dans "_Envoi contrat — erreur" (visible Direction/closer).
# Jamais bloquant : sans jeton, sans record_id ou en cas d'erreur Airtable, le
# contrat part quand meme ; le detail est visible dans GET /status/<doc_id>.
AT_FIELD_DOC_ID = "_PandaDoc ID"
AT_FIELD_LINK = "🔒 Lien de signature (client) — Direction"   # visible Direction uniquement (decision Eloi 15/09)
AT_FIELD_STATUT = "Statut contrat"
AT_FIELD_ERREUR = "_Envoi contrat — erreur"
AT_STATUT_ENVOYE = "Envoyé"
AT_STATUT_ERREUR = "⚠️ Erreur envoi"
# statuts que l'on ne doit JAMAIS ecraser par "Envoyé" (deja plus avances)
AT_STATUTS_AVANCES = ("En attente signature", "Lu", "Signé", "Refusé", "Annulé",
                      "Résilié", "Litige", "Expiré")


def _at_headers():
    token = os.environ.get("AIRTABLE_TOKEN")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"} if token else None


def airtable_get_fields(record_id, fields=None):
    """Lit une vente. Renvoie le dict "fields" ou None (jamais d'exception)."""
    h = _at_headers()
    if not h or not record_id:
        return None
    try:
        # NB : l'endpoint "un enregistrement" n'accepte pas fields[] (422) -> on lit
        # tout l'enregistrement et on filtre ensuite (v24.2).
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_VENTES}/{record_id}",
                         headers=h, timeout=15)
        if r.status_code >= 400:
            return None
        allf = r.json().get("fields", {}) or {}
        if fields:
            return {k: allf.get(k) for k in fields}
        return allf
    except Exception:
        return None


def airtable_patch(record_id, fields):
    """Met a jour des champs de la vente. Renvoie (ok, detail). typecast=True :
    les valeurs de liste deroulante sont acceptees par leur libelle."""
    h = _at_headers()
    if not h:
        return False, "AIRTABLE_TOKEN absent"
    if not record_id:
        return False, "record_id absent"
    try:
        r = requests.patch(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_VENTES}/{record_id}",
                           headers=h, data=json.dumps({"fields": fields, "typecast": True}),
                           timeout=20)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:300]}"
        return True, "ok"
    except Exception as e:
        return False, str(e)


def fetch_client_link(doc_id, key, client_email, tries=6):
    """Apres l'envoi, relit le document et renvoie le lien de signature
    personnel du signataire (recipients[].shared_link). PandaDoc le renseigne de
    facon asynchrone : on reessaie quelques secondes. None si introuvable."""
    want = (client_email or "").strip().lower()
    for _ in range(tries):
        try:
            det = requests.get(f"{PANDADOC}/documents/{doc_id}/details",
                               headers=_headers(key), timeout=30).json()
            best = None
            for rc in det.get("recipients") or []:
                if str(rc.get("recipient_type") or "").upper() == "CC":
                    continue
                link = rc.get("shared_link")
                if not link:
                    continue
                if (rc.get("email") or "").strip().lower() == want:
                    return link
                best = best or link       # signataire sans correspondance email exacte
            if best:
                return best
        except Exception:
            pass
        time.sleep(2)
    return None


def report_to_airtable(doc_id, record_id, client_email, key, job):
    """Ecrit dans la vente le resultat REEL du montage/envoi (voir v24)."""
    res = {"record_id": record_id}
    if not record_id:
        res["skipped"] = "record_id absent"
        job["airtable"] = res
        return
    if not os.environ.get("AIRTABLE_TOKEN"):
        res["skipped"] = "AIRTABLE_TOKEN absent"
        job["airtable"] = res
        return
    try:
        stage = job.get("stage")
        fields = {AT_FIELD_DOC_ID: doc_id}
        if stage == "sent":
            link = fetch_client_link(doc_id, key, client_email)
            res["link"] = link
            if link:
                fields[AT_FIELD_LINK] = link
            fields[AT_FIELD_ERREUR] = ""
            cur = airtable_get_fields(record_id, [AT_FIELD_STATUT])
            if cur is None:
                # lecture impossible : on ne touche pas au statut (v24.2)
                res["statut_avant"] = "(lecture impossible)"
            else:
                statut = (cur.get(AT_FIELD_STATUT) or "").strip()
                if statut not in AT_STATUTS_AVANCES:
                    fields[AT_FIELD_STATUT] = AT_STATUT_ENVOYE
                res["statut_avant"] = statut
        elif stage == "done":
            # brouillon volontaire (send=false) : rien a signaler
            fields[AT_FIELD_ERREUR] = ""
        else:
            err = f"{stage or 'inconnu'} : {job.get('error') or 'erreur inconnue'}"
            fields[AT_FIELD_STATUT] = AT_STATUT_ERREUR
            fields[AT_FIELD_ERREUR] = err[:250]
        ok, detail = airtable_patch(record_id, fields)
        res.update(ok=ok, detail=detail, fields=list(fields.keys()))
    except Exception as e:
        res.update(ok=False, detail=str(e))
    job["airtable"] = res


# suivi en memoire du montage en tache de fond (pour /status)
JOBS = {}


def split_body(pdf_bytes: bytes):
    """Separe le PDF autour de la page signature (reperee par le tag).
    Retourne (corps_avant_signature, annexes_apres_signature, sig_idx)."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    sig_idx = None
    for i, pg in enumerate(src):
        if pg.search_for(SIG_TAG):
            sig_idx = i
            break
    if sig_idx is None:
        sig_idx = src.page_count  # pas de page signature -> tout est corps
    body = fitz.open()
    if sig_idx > 0:
        body.insert_pdf(src, from_page=0, to_page=sig_idx - 1)
    annexe = fitz.open()
    if sig_idx + 1 <= src.page_count - 1:
        annexe.insert_pdf(src, from_page=sig_idx + 1, to_page=src.page_count - 1)

    def dump(dd):
        if dd.page_count == 0:
            return None
        b = io.BytesIO(); dd.save(b, garbage=3, deflate=True); return b.getvalue()
    try:
        return dump(body), dump(annexe), sig_idx
    finally:                                         # v25 : liberation memoire MuPDF
        for dd in (body, annexe, src):
            try:
                dd.close()
            except Exception:
                pass
        _free_mupdf()


MAX_IMG_W = 900          # largeur max des images bitmap apres reduction
JPEG_QUALITY = 80        # qualite JPEG des images recompressees
FIDELITY_DPI = 50        # resolution du controle de fidelite page a page
FIDELITY_MAX = 6.0       # ecart moyen tolere (0-255) avant retour a l'original


def _free_mupdf():
    """v25 — CAUSE DES PLANTAGES "Ran out of memory (used over 512MB)" du 16/09/2026
    (5 redemarrages dans la journee, contrats en erreur 502 pendant ~1 min a chaque
    fois) : MuPDF garde en cache ("store") les images decodees de chaque PDF traite
    (couverture 2400 px = ~11 Mo decodee). Le cache n'est jamais vide entre deux
    contrats, la memoire grimpe de ~25 Mo par contrat jusqu'a la limite de
    l'instance. Mesure locale : 20 contrats -> 617 Mo sans purge, 130 Mo avec.
    On vide donc le cache et on force le ramasse-miettes apres chaque traitement."""
    try:
        fitz.TOOLS.store_shrink(100)
    except Exception:
        pass
    gc.collect()


def _recompress_images(doc):
    """Recompresse chaque image bitmap en JPEG, en la reduisant si elle est
    beaucoup plus grande que son affichage. On remplace UNIQUEMENT l'objet
    image ; les ressources de page (degrades, motifs, calques) sont laissees
    intactes -- c'est ce qui distingue cette methode de doc.rewrite_images(),
    qui detruisait les Pattern de la page de couverture (v15/v16)."""
    seen = set()
    for pno in range(doc.page_count):
        page = doc[pno]
        for im in page.get_images(full=True):
            xref, smask = im[0], im[1]
            if xref in seen:
                continue
            seen.add(xref)
            if smask:
                continue                      # transparence : on ne touche pas
            try:
                old = len(doc.xref_stream_raw(xref))
            except Exception:
                old = 0
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                k = 0
                while pix.width // (2 ** (k + 1)) >= MAX_IMG_W:
                    k += 1
                if k:
                    pix.shrink(k)
                new = pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
                pix = None
            except Exception:
                continue
            if not new or (old and len(new) >= old):
                continue
            try:
                page.replace_image(xref, stream=new)
            except Exception:
                pass


def _fidelity_ok(orig_bytes, new_bytes):
    """Compare le rendu page a page de l'original et du compresse.
    Retourne False des qu'une page s'ecarte visiblement -> on gardera
    l'original. Cout mesure : ~0,5 s pour un contrat de 13 pages."""
    a = b = None
    try:
        a = fitz.open(stream=orig_bytes, filetype="pdf")
        b = fitz.open(stream=new_bytes, filetype="pdf")
        if a.page_count != b.page_count:
            return False
        for i in range(a.page_count):
            sa = a[i].get_pixmap(dpi=FIDELITY_DPI).samples
            sb = b[i].get_pixmap(dpi=FIDELITY_DPI).samples
            if len(sa) != len(sb):
                return False
            n = len(sa)
            step = 97                          # echantillonnage : ~1 octet sur 97
            tot = 0
            cnt = 0
            for j in range(0, n, step):
                tot += abs(sa[j] - sb[j])
                cnt += 1
            if cnt and (tot / cnt) > FIDELITY_MAX:
                return False
        return True
    except Exception:
        return False
    finally:                                         # v25 : liberation memoire MuPDF
        for dd in (a, b):
            try:
                dd.close()
            except Exception:
                pass


def compress_pdf(pdf_bytes: bytes):
    """v17 — Compresse le PDF (images bitmap recompressees en JPEG et reduites,
    polices sous-ensembles, flux degonfles) pour accelerer l'upload PandaDoc et
    la reception cote client.

    v17 corrige la regression v15/v16 : doc.rewrite_images() faisait perdre les
    ressources Pattern de la page de couverture (degrade), au point que
    PandaDoc affichait la couverture entierement blanche. On recompresse
    desormais les images une par une, sans toucher aux ressources de page, et
    un controle de fidelite page a page renvoie l'original au moindre doute.
    En cas de probleme, on renvoie le PDF d'origine (aucune regression
    possible)."""
    doc = chk = None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n_pages = doc.page_count
        _recompress_images(doc)
        try:
            doc.subset_fonts()
        except Exception:
            pass
        b = io.BytesIO()
        doc.save(b, garbage=4, deflate=True)
        out = b.getvalue()
        b = None
        # garde-fou 1 : resultat vide ou plus lourd -> original
        if not out or len(out) >= len(pdf_bytes):
            return pdf_bytes, len(pdf_bytes), len(pdf_bytes)
        # garde-fou 2 : nombre de pages inchange
        chk = fitz.open(stream=out, filetype="pdf")
        if chk.page_count != n_pages:
            return pdf_bytes, len(pdf_bytes), len(pdf_bytes)
        # garde-fou 3 : le rendu de chaque page doit rester identique
        if not _fidelity_ok(pdf_bytes, out):
            return pdf_bytes, len(pdf_bytes), len(pdf_bytes)
        return out, len(pdf_bytes), len(out)
    except Exception:
        return pdf_bytes, len(pdf_bytes), len(pdf_bytes)
    finally:                                         # v25 : liberation memoire MuPDF
        for dd in (doc, chk):
            try:
                dd.close()
            except Exception:
                pass
        _free_mupdf()


def _headers(key):
    return {"Authorization": f"API-Key {key}"}


# v25 — resilience : les appels PandaDoc qui echouent de facon TRANSITOIRE
# (502/503/504 passerelle, 429 quota, 409 "document isn't ready for a status
# transition yet") sont rejoues avec un delai croissant au lieu de terminer en
# "Erreur envoi" (constat du 16/09/2026 : Kalwele = add-signature 502 HTML,
# Naert = send 409 "please consider implementing a retry"). Chaque echec cote
# closer coutait 5 a 10 min de re-saisie.
RETRY_STATUSES = (409, 429, 500, 502, 503, 504)
RETRY_DELAYS = (3, 6, 12, 20, 30)          # secondes entre deux essais


def _pd_post(url, key, job=None, doc_id=None, **kw):
    """POST PandaDoc avec rejeu automatique. Renvoie la derniere reponse (ou une
    reponse factice en cas d'exception reseau persistante)."""
    last = None
    hdrs = {**_headers(key), **(kw.pop("extra_headers", None) or {})}
    for i, delay in enumerate(RETRY_DELAYS + (None,)):
        try:
            r = requests.post(url, headers=hdrs, **kw)
            last = r
            if r.status_code < 400 or r.status_code not in RETRY_STATUSES:
                return r
        except Exception as e:                       # coupure reseau / timeout
            last = _FakeResp(599, str(e))
        if delay is None:
            break
        if job is not None:
            job["retries"] = job.get("retries", 0) + 1
            job["last_retry"] = f"HTTP {last.status_code} -> nouvel essai dans {delay}s"
        if doc_id and last.status_code == 409:
            _wait_draft(doc_id, key, tries=10)       # "not ready" : on attend le brouillon
        time.sleep(delay)
    return last


class _FakeResp:
    def __init__(self, status_code, text):
        self.status_code, self.text = status_code, text

    def json(self):
        return {}


JOBS_TTL = 2 * 3600        # les suivis de montage sont purges apres 2 h
RELAY_MAX = 3              # le relais memoire ne garde que les 3 derniers fichiers


def _purge_jobs():
    now = time.time()
    for k in [k for k, v in JOBS.items() if now - v.get("ts", now) > JOBS_TTL]:
        JOBS.pop(k, None)


POLL_S = 1.0             # v25.1 : intervalle de sondage PandaDoc (1,5 s avant)


def _wait_draft(doc_id, key, tries=60):
    """Attend que le document soit un brouillon pret (statut document.draft).
    v25.1 : on interroge D'ABORD, on dort ensuite. Avant, chaque appel dormait
    1,5 s avant meme de regarder, et la chaine en fait 5 -> ~8 s perdus par
    contrat alors que le document etait deja pret."""
    for i in range(tries):
        if i:
            time.sleep(POLL_S)
        try:
            st = requests.get(f"{PANDADOC}/documents/{doc_id}/details",
                              headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            st = None
        if st == "document.draft":
            return True
    return False


def _wait_section(doc_id, up_id, key, tries=90):
    for i in range(tries):
        if i:
            time.sleep(POLL_S)
        try:
            st = requests.get(f"{PANDADOC}/documents/{doc_id}/sections/uploads/{up_id}",
                              headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            st = None
        if st and "PROCESSED" in str(st).upper():
            return True
    return False


# v25.1 — chronometrage de bout en bout (objectif Eloi : contrat envoye en < 80 s).
# Chaque envoi reussi est memorise (30 derniers) et visible dans GET /health.
LAST_SENDS = []
LAST_SENDS_MAX = 30


def _note_send(doc_id, job):
    try:
        t0 = job.get("t0")
        if not t0:
            return
        d = round(time.time() - t0, 1)
        job["duree_s"] = d
        from datetime import datetime, timezone
        LAST_SENDS.append({"quand": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "document_id": doc_id, "record_id": job.get("record_id"),
                           "duree_s": d, "creation_s": job.get("creation_s"),
                           "retries": job.get("retries", 0)})
        while len(LAST_SENDS) > LAST_SENDS_MAX:
            LAST_SENDS.pop(0)
        print(f"[timing] {doc_id} vente={job.get('record_id')} envoye en {d} s "
              f"(creation {job.get('creation_s')} s, rejeux {job.get('retries', 0)})", flush=True)
    except Exception:
        pass


def assemble_bg(doc_id, key, template_uuid, recipient, annexe_pdf,
                subject=None, do_send=True, message=None, fields=None,
                tokens=None, record_id=None):
    """Tache de fond : ajoute la page signature (modele) puis les annexes,
    puis ENVOIE le contrat au signataire (plus de brouillon).
    v24 : quoi qu'il arrive, le resultat reel est ecrit dans la vente Airtable."""
    job = JOBS.setdefault(doc_id, {})
    job.setdefault("ts", time.time())
    try:
        _assemble_and_send(doc_id, key, template_uuid, recipient, annexe_pdf,
                           subject, do_send, message, fields, tokens, job)
    except Exception as e:
        job.update(stage="error", error=str(e), trace=traceback.format_exc()[-500:])
    finally:
        try:
            report_to_airtable(doc_id, record_id, recipient.get("email"), key, job)
        except Exception as e:
            job["airtable"] = {"ok": False, "detail": str(e)}
        annexe_pdf = None                            # v25 : memoire rendue au plus tot
        _purge_jobs()
        gc.collect()


def _assemble_and_send(doc_id, key, template_uuid, recipient, annexe_pdf,
                       subject, do_send, message, fields, tokens, job):
    try:
        job["stage"] = "wait-body"
        if not _wait_draft(doc_id, key):
            job.update(stage="error", error="corps pas pret a temps"); return

        # page signature (modele natif, cases obligatoires) -> corps API JSON
        job["stage"] = "add-signature"
        sec = {"template_uuid": template_uuid, "name": "Validation & signature",
               "recipients": [dict(recipient, role=TEMPLATE_ROLE)]}
        if fields:
            # pre-remplit les champs du modele (ex: client_nom sous "LE CLIENT")
            sec["fields"] = fields
        if tokens:
            # v18 — variables texte statiques [client_nom]/[date_envoi] :
            # imprimees telles quelles, NON modifiables par le signataire.
            # Ignorees si le modele ne contient pas la variable (sans danger).
            sec["tokens"] = tokens
        rr = _pd_post(f"{PANDADOC}/documents/{doc_id}/sections/uploads", key, job=job, doc_id=doc_id,
                      extra_headers={"Content-Type": "application/json"},
                      data=json.dumps(sec), timeout=90)
        if rr.status_code >= 400:
            job.update(stage="error", error=f"add-signature {rr.status_code}: {rr.text[:300]}"); return
        _wait_section(doc_id, rr.json().get("uuid"), key)
        _wait_draft(doc_id, key)

        # annexes en DERNIER (section fichier)
        if annexe_pdf:
            job["stage"] = "add-annexe"
            ra = _pd_post(f"{PANDADOC}/documents/{doc_id}/sections/uploads", key, job=job, doc_id=doc_id,
                          files={"file": ("annexes.pdf", annexe_pdf, "application/pdf")},
                          data={"data": json.dumps({"name": "Annexes"})}, timeout=90)
            annexe_pdf = None                        # v25 : on libere les octets tout de suite
            if ra.status_code < 400:
                _wait_section(doc_id, ra.json().get("uuid"), key)
                _wait_draft(doc_id, key)
            else:
                job.update(stage="error", error=f"add-annexe {ra.status_code}: {ra.text[:300]}"); return

        # retirer le prefixe "[DEV]" impose par la cle sandbox (rename en brouillon)
        job["stage"] = "rename"
        try:
            _wait_draft(doc_id, key)
            cur = requests.get(f"{PANDADOC}/documents/{doc_id}/details",
                               headers=_headers(key), timeout=30).json()
            nm = (cur.get("name") or "")
            if nm.startswith("[DEV]"):
                requests.patch(f"{PANDADOC}/documents/{doc_id}",
                               headers={**_headers(key), "Content-Type": "application/json"},
                               data=json.dumps({"name": nm.replace("[DEV]", "", 1).strip()}),
                               timeout=30)
        except Exception:
            pass  # non bloquant : au pire le prefixe reste

        # montage termine : le document est un brouillon complet
        if not do_send:
            job["stage"] = "done"
            return

        # ENVOI AUTOMATIQUE : on envoie le contrat au signataire (fini le brouillon)
        job["stage"] = "send"
        _wait_draft(doc_id, key)  # s'assurer que le doc est bien pret a etre envoye
        send_body = {
            "silent": False,  # False = PandaDoc envoie l'email au signataire
            "subject": subject or "Votre contrat Arthaud Immobilier Academie",
            "message": message or ("Bonjour,\n\nVoici votre contrat à signer "
                                   "électroniquement.\n\nBien cordialement,\n"
                                   "Arthaud Immobilier Académie"),
        }
        sd = _pd_post(f"{PANDADOC}/documents/{doc_id}/send", key, job=job, doc_id=doc_id,
                      extra_headers={"Content-Type": "application/json"},
                      data=json.dumps(send_body), timeout=90)
        if sd.status_code >= 400:
            job.update(stage="error",
                       error=f"send {sd.status_code}: {sd.text[:300]}"); return
        job["stage"] = "sent"
        _note_send(doc_id, job)                      # v25.1 : chrono de bout en bout
    except Exception as e:
        job.update(stage="error", error=str(e), trace=traceback.format_exc()[-500:])


@app.post("/create-draft")
def create_draft():
    t0 = time.time()                                 # v25.1 : depart du chrono
    try:
        d = request.get_json(force=True) or {}
        key = os.environ.get("PANDADOC_API_KEY")
        if not key:
            return jsonify({"ok": False, "stage": "config", "error": "PANDADOC_API_KEY manquante"}), 500

        ctype = (d.get("contract_type") or "b2b").lower()
        # routage par produit : "Incubateur ..." -> modeles signature Incubateur
        produit_brut = (d.get("produit") or "").strip().lower()
        prod_key = detect_prod_key(produit_brut)
        # override explicite (tests) : "template_uuid" dans le body
        template_uuid = (d.get("template_uuid") or "").strip() or None
        if not template_uuid:
            template_uuid = TEMPLATES.get(f"{ctype}:{prod_key}") if prod_key else None
        if not template_uuid:
            template_uuid = TEMPLATES.get(ctype)
        if not template_uuid:
            return jsonify({"ok": False, "stage": "input",
                            "error": f"contract_type inconnu: {ctype} (attendu b2b ou b2c)"}), 400
        if not d.get("pdf_url"):
            return jsonify({"ok": False, "stage": "input", "error": "pdf_url manquant"}), 400
        if not d.get("client_email"):
            return jsonify({"ok": False, "stage": "input", "error": "client_email manquant"}), 400

        recipient = {
            "email": d["client_email"],
            "first_name": d.get("client_first_name", ""),
            "last_name": d.get("client_last_name", ""),
        }
        # v20 — les 10 Zaps n'envoient PAS client_first_name / client_last_name.
        # Sans nom, PandaDoc cree le signataire "vide" et retombe sur le contact
        # deja enregistre pour cet email => le CERTIFICAT DE SIGNATURE (derniere
        # page du PDF signe) affiche un nom errone, alors que le corps du contrat
        # est correct. On deduit donc prenom/nom de "client_nom".
        if not recipient["first_name"] and not recipient["last_name"]:
            _parts = (d.get("client_nom") or "").strip().split()
            if _parts:
                recipient["first_name"] = _parts[0]
                recipient["last_name"] = " ".join(_parts[1:])

        # v18 — CC closer : le vendeur recoit une copie du contrat envoye.
        # Le Zap passe "closer_email" (lookup "Email vendeur" de la vente).
        # Sans role ni champ assigne, PandaDoc le traite comme destinataire CC.
        closer_email = (d.get("closer_email") or "").strip()
        cc_recipients = []
        if closer_email and closer_email.lower() != d["client_email"].strip().lower():
            cc_recipients.append({
                "email": closer_email,
                "first_name": (d.get("closer_prenom") or "").strip(),
                "last_name": "",
                "recipient_type": "CC",
            })

        # 1) Telecharger le PDF PDFMonkey complet
        pdf = None
        for essai in range(3):                       # v25 : 3 essais (PDFMonkey / reseau)
            try:
                rp = requests.get(d["pdf_url"], timeout=60)
                if rp.status_code < 400 and rp.content[:4] == b"%PDF":
                    pdf = rp.content
                    break
                err = f"HTTP {rp.status_code}"
            except Exception as e:
                err = str(e)
            time.sleep(2 * (essai + 1))
        if pdf is None:
            return jsonify({"ok": False, "stage": "download", "error": err}), 502

        # 1bis) v15 — Compression du PDF (images 120 dpi JPEG q75, polices
        #        sous-ensembles). Sans risque : retombe sur l'original si echec.
        pdf, size_before, size_after = compress_pdf(pdf)

        # 2) Corps (avant signature) + annexes (apres signature)
        body_pdf, annexe_pdf, sig_idx = split_body(pdf)
        if not body_pdf:
            return jsonify({"ok": False, "stage": "split", "error": "corps vide"}), 500

        # 3) Creer le document PandaDoc a partir du corps (RAPIDE)
        #    Nom du document : "Contrat {Produit} - {Prenom Nom}" si produit /
        #    client_nom sont fournis par le Zap ; sinon document_name ; sinon defaut.
        produit = (d.get("produit") or "").strip()
        client_nom = (d.get("client_nom") or "").strip()
        if d.get("document_name"):
            doc_name = d["document_name"]
        elif produit or client_nom:
            doc_name = ("Contrat " + produit).strip()
            if client_nom:
                doc_name += " - " + client_nom
        else:
            doc_name = f"Contrat Mastermind {ctype.upper()}"
        # v19 — metadata "record_id" : CLE DE RECONCILIATION. Le Zap
        # "PDC — 3. Contrat signe -> Airtable" retrouve la vente via le champ
        # "Metadata Record Id". Sans elle, la signature ne remonte JAMAIS dans
        # Airtable (statut "Signe" absent => commissions sous-evaluees).
        # Le Zap d'envoi passe "record_id" (deja fourni par l'automation Airtable).
        record_id = (d.get("record_id") or "").strip()
        meta = {"name": doc_name,
                "recipients": [dict(recipient, role="client")] + cc_recipients}
        if record_id:
            meta["metadata"] = {"record_id": record_id}
        r = requests.post(f"{PANDADOC}/documents", headers=_headers(key),
                          files={"file": ("corps.pdf", body_pdf, "application/pdf")},
                          data={"data": json.dumps(meta)}, timeout=90)
        if r.status_code >= 400:
            return jsonify({"ok": False, "stage": "create-body",
                            "http_status": r.status_code, "error": r.text[:800]}), 502
        doc_id = r.json()["id"]

        # v24 — l'ID du document est ecrit TOUT DE SUITE dans la vente : si le
        # montage en fond meurt, on retrouve le brouillon orphelin depuis Airtable.
        at_first = None
        if record_id:
            at_first = airtable_patch(record_id, {AT_FIELD_DOC_ID: doc_id})

        # 4) Lancer le montage des sections EN TACHE DE FOND et repondre tout de suite
        #    do_send=True par defaut => le contrat est ENVOYE (pas juste un brouillon).
        #    Passer "send": false dans le body pour rester en brouillon (tests).
        do_send = bool(d.get("send", True))

        # Message d'email personnalise (texte historique du zap Mastermind B2C)
        prenom_client = (d.get("client_first_name") or "").strip()
        if not prenom_client and client_nom:
            # fallback : 1er mot du nom du contrat ("Eloi TEST" -> "Eloi")
            prenom_client = client_nom.split()[0]
        closer_prenom = (d.get("closer_prenom") or "").strip()
        programme = PROGRAMMES.get(prod_key, "le Mastermind")
        salutation = f"Bonjour {prenom_client}," if prenom_client else "Bonjour,"
        if closer_prenom:
            signature_mail = (closer_prenom + "\n"
                              "Chargé de recrutement — Arthaud Immobilier Académie")
        else:
            signature_mail = "Arthaud Immobilier Académie"
        email_message = (
            f"{salutation}\n\n"
            f"Comme convenu lors de notre échange, je vous adresse votre contrat "
            f"pour {programme} Arthaud Immobilier.\n\n"
            "Vous y retrouverez l'ensemble des éléments dont nous avons parlé : "
            "votre programme, votre échéancier et votre date de démarrage.\n\n"
            "La signature s'effectue électroniquement, en quelques instants.\n\n"
            "Je reste à votre disposition pour tout complément d'information.\n\n"
            "Bien cordialement,\n\n"
            f"{signature_mail}"
        )

        # pre-remplissage des champs du modele (nom du client + date d'envoi)
        # date d'envoi : parametre "date_envoi" (JJ/MM/AAAA) sinon date du jour (Paris)
        date_envoi = (d.get("date_envoi") or "").strip()
        if not date_envoi:
            try:
                from zoneinfo import ZoneInfo
                from datetime import datetime
                date_envoi = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y")
            except Exception:
                from datetime import datetime
                date_envoi = datetime.utcnow().strftime("%d/%m/%Y")
        prefill = {}
        # tous les modeles de page signature (Mastermind v2, Elite, Incubateur v4,
        # Formation, Starter) portent les champs client_nom + date_envoi.
        if client_nom:
            prefill["client_nom"] = {"value": client_nom}
        prefill["date_envoi"] = {"value": date_envoi}

        # v23 (09/09/2026, decision Eloi) — CASES FORCEES A "OUI" POUR TOUS LES
        # CONTRATS : acces immediat, renonciation (B2C) et "j'ai lu et j'accepte"
        # arrivent cochees quoi qu'ait saisi le closer ; elles sont "Champ requis"
        # dans les 3 modeles PandaDoc, donc le client ne peut pas signer sans.
        # La lecture Airtable v22 (fetch_visio_choices) est conservee pour un
        # eventuel retour arriere mais n'est plus consultee.
        if FORCE_CHECKBOXES:
            visio = {"acces": True, "renonciation": (ctype == "b2c"), "source": "forced"}
        else:
            visio = None
            if "acces_immediat" in d or "renonciation" in d:
                _a = _truthy(d.get("acces_immediat"))
                visio = {"acces": _a, "renonciation": _truthy(d.get("renonciation")) and _a,
                         "source": "body"}
            else:
                visio = fetch_visio_choices(record_id)
                if visio:
                    visio["source"] = "airtable"
        if visio:
            prefill[MERGE_ACCES] = {"value": bool(visio["acces"])}
            if ctype == "b2c":
                prefill[MERGE_RENONCIATION] = {"value": bool(visio["renonciation"])}
            prefill[MERGE_CONDITIONS] = {"value": True}

        # v18 — memes valeurs egalement transmises en tokens (variables texte
        # statiques) pour les modeles qui utilisent [client_nom]/[date_envoi]
        # au lieu de champs remplissables.
        sec_tokens = [{"name": "date_envoi", "value": date_envoi}]
        if client_nom:
            sec_tokens.append({"name": "client_nom", "value": client_nom})

        _purge_jobs()
        JOBS[doc_id] = {"stage": "queued", "ts": time.time(), "contract_type": ctype, "will_send": do_send,
                        "visio_choices": visio, "record_id": record_id or None,
                        "airtable_doc_id_write": (at_first[1] if at_first else "record_id absent"),
                        "t0": t0, "creation_s": round(time.time() - t0, 1)}   # v25.1 chrono
        pdf = body_pdf = None                        # v25 : le corps est deja chez PandaDoc
        gc.collect()
        threading.Thread(target=assemble_bg,
                         args=(doc_id, key, template_uuid, recipient, annexe_pdf),
                         kwargs={"subject": meta["name"], "do_send": do_send,
                                 "message": email_message,
                                 "fields": (prefill or None),
                                 "tokens": sec_tokens,
                                 "record_id": record_id},
                         daemon=True).start()

        return jsonify({"ok": True, "document_id": doc_id,
                        "status": "assembling+send" if do_send else "assembling",
                        "contract_type": ctype, "signature_page_index": sig_idx,
                        "visio_choices": visio, "record_id": record_id or None,
                        "pdf_bytes_before": size_before, "pdf_bytes_after": size_after,
                        "edit_url": f"https://app.pandadoc.com/a/#/documents/{doc_id}"})
    except Exception as e:
        return jsonify({"ok": False, "stage": "unhandled",
                        "error": str(e), "trace": traceback.format_exc()[-800:]}), 500


# Relais temporaire : le navigateur pousse une URL signee, le serveur telecharge,
# et le fichier est recuperable via GET /relay/<name> (memoire, non persistant).
RELAY = {}


@app.post("/relay")
def relay_set():
    d = request.get_json(force=True) or {}
    u = d.get("url"); n = d.get("name", "f")
    try:
        c = requests.get(u, timeout=90).content
        RELAY[n] = c
        _trim_relay()
        return jsonify({"ok": True, "name": n, "bytes": len(c)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.post("/relay-up/<n>")
def relay_up(n):
    RELAY[n] = request.get_data()
    _trim_relay()
    return jsonify({"ok": True, "name": n, "bytes": len(RELAY[n])})


def _trim_relay():
    """v25 : le relais est un cache temporaire, pas un stockage — on ne garde
    que les RELAY_MAX derniers fichiers (sinon la memoire grimpe jusqu'a l'OOM)."""
    while len(RELAY) > RELAY_MAX:
        RELAY.pop(next(iter(RELAY)), None)


@app.get("/relay/<n>")
def relay_get(n):
    from flask import Response
    c = RELAY.get(n)
    if c is None:
        return jsonify({"ok": False, "error": "inconnu"}), 404
    r = Response(c, mimetype="application/pdf")
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


@app.get("/status/<doc_id>")
def status(doc_id):
    key = os.environ.get("PANDADOC_API_KEY")
    pd_status = None
    if key:
        try:
            pd_status = requests.get(f"{PANDADOC}/documents/{doc_id}/details",
                                     headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            pd_status = None
    return jsonify({"document_id": doc_id, "assembly": JOBS.get(doc_id, {}),
                    "pandadoc_status": pd_status,
                    "edit_url": f"https://app.pandadoc.com/a/#/documents/{doc_id}"})


@app.get("/airtable-check")
def airtable_check():
    """v24 — Verifie, SANS exposer le jeton, que le service peut ecrire dans la
    base : scopes du jeton (whoami) + acces en lecture a la table Ventes.
    Le retour Airtable exige le scope data.records:write."""
    token = os.environ.get("AIRTABLE_TOKEN")
    if not token:
        return jsonify({"ok": False, "token": "absent"}), 200
    out = {"token": "present"}
    try:
        w = requests.get("https://api.airtable.com/v0/meta/whoami",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if w.status_code < 400:
            # NB : Airtable ne renvoie "scopes" que pour un jeton OAuth, jamais pour
            # un jeton d'acces personnel -> le vrai test d'ecriture est ci-dessous.
            out["scopes"] = w.json().get("scopes")
            out["whoami"] = "ok"
        else:
            out["whoami"] = f"HTTP {w.status_code}"
    except Exception as e:
        out["whoami"] = str(e)
    # v24.1 — test d'ecriture reel et sans effet : ?write_test=rec... relit le
    # champ "_Envoi contrat — erreur" de cette vente et le reecrit a l'identique.
    rid = (request.args.get("write_test") or "").strip()
    if rid.startswith("rec"):
        cur = airtable_get_fields(rid, [AT_FIELD_ERREUR])
        if cur is None:
            out["peut_ecrire"] = False
            out["write_test"] = "lecture de la vente impossible"
        else:
            ok, detail = airtable_patch(rid, {AT_FIELD_ERREUR: cur.get(AT_FIELD_ERREUR) or ""})
            out["peut_ecrire"] = ok
            out["write_test"] = detail
    else:
        out["peut_ecrire"] = None
        out["write_test"] = "ajouter ?write_test=<record_id d'une vente de test> pour tester l'ecriture"
    try:
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_VENTES}",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"maxRecords": 1, "fields[]": AT_FIELD_DOC_ID}, timeout=15)
        out["lecture_ventes"] = "ok" if r.status_code < 400 else f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        out["lecture_ventes"] = str(e)
    out["ok"] = (out.get("peut_ecrire") is not False) and out.get("lecture_ventes") == "ok"
    out["champs_attendus"] = [AT_FIELD_DOC_ID, AT_FIELD_LINK, AT_FIELD_STATUT, AT_FIELD_ERREUR]
    return jsonify(out), 200


@app.post("/backfill")
def backfill():
    """v24.1 — Rattrapage : pour les ventes dont le contrat est parti AVANT la
    v24 (ou pendant une panne d'ecriture Airtable), retrouve le document
    PandaDoc via sa metadata record_id et ecrit _PandaDoc ID + lien de
    signature dans la vente. Ne touche PAS au Statut contrat.
    Body JSON : {"record_ids": ["rec...", ...], "dry": true|false}
    Reserve a l'operateur (Eloi) : n'envoie rien, n'ecrit que si dry=false."""
    key = os.environ.get("PANDADOC_API_KEY")
    if not key:
        return jsonify({"ok": False, "error": "PANDADOC_API_KEY manquante"}), 500
    d = request.get_json(force=True) or {}
    ids = [str(x).strip() for x in (d.get("record_ids") or []) if str(x).strip().startswith("rec")]
    dry = bool(d.get("dry", True))
    out = []
    for rid in ids[:100]:
        item = {"record_id": rid}
        try:
            # v24.3 — tri descendant = prefixe "-" (le parametre "asc" n'existe
            # pas dans l'API PandaDoc et faisait echouer la requete en 400)
            r = requests.get(f"{PANDADOC}/documents",
                             headers=_headers(key),
                             params={"metadata_record_id": rid, "count": 20,
                                     "order_by": "-date_created"},
                             timeout=30)
            if r.status_code >= 400:
                item["error"] = f"PandaDoc HTTP {r.status_code} : {r.text[:200]}"
                out.append(item); continue
            docs = r.json().get("results") or []
            # on ignore les brouillons jamais envoyes ; on prend le plus recent envoye
            sent = [x for x in docs if str(x.get("status") or "") not in ("document.draft", "document.uploaded")]
            doc = (sent or docs or [None])[0]
            if not doc:
                item["skipped"] = "aucun document PandaDoc avec cette metadata"
                out.append(item); continue
            item["document_id"] = doc.get("id"); item["pandadoc_status"] = doc.get("status")
            item["candidats"] = len(docs)
            fields = {AT_FIELD_DOC_ID: doc.get("id")}
            cur = airtable_get_fields(rid, [AT_FIELD_STATUT]) or {}
            client_email = None
            if str(doc.get("status") or "") != "document.draft":
                link = fetch_client_link(doc.get("id"), key, client_email, tries=2)
                item["link"] = link
                if link:
                    fields[AT_FIELD_LINK] = link
            else:
                fields[AT_FIELD_ERREUR] = "brouillon PandaDoc jamais envoye (rattrapage)"
            item["fields"] = list(fields.keys())
            if dry:
                item["dry"] = True
            else:
                ok, detail = airtable_patch(rid, fields)
                item.update(ok=ok, detail=detail)
        except Exception as e:
            item["error"] = str(e)
        out.append(item)
    return jsonify({"ok": True, "dry": dry, "count": len(out), "results": out}), 200


@app.get("/")
def health():
    airtable = "oui" if os.environ.get("AIRTABLE_TOKEN") else "NON"
    mode = ("cases TOUJOURS cochees (forcees)" if FORCE_CHECKBOXES
            else f"cases pre-cochees d'apres la visio, lecture Airtable: {airtable}")
    return (f"Contrat PandaDoc service OK (async v25.1 - {mode} - 3 modeles signature "
            "consolides - CC closer + metadata record_id + nom signataire - retour "
            f"Airtable apres envoi reel: ID doc + lien de signature + statut, jeton Airtable: {airtable} - "
            "v25: rejeu auto des erreurs PandaDoc transitoires + liberation memoire - "
            "v25.1: sondage PandaDoc sans attente inutile + chrono des envois, voir /timings)"), 200


@app.get("/timings")
def timings():
    """v25.1 — Les 30 derniers contrats envoyes avec leur duree de bout en bout
    (reception de la demande Zapier -> statut 'sent' PandaDoc), en secondes."""
    try:
        import statistics
        d = [x["duree_s"] for x in LAST_SENDS if x.get("duree_s") is not None]
        stats = {"n": len(d), "mediane_s": round(statistics.median(d), 1) if d else None,
                 "max_s": max(d) if d else None,
                 "sous_80s": sum(1 for x in d if x <= 80) if d else 0}
    except Exception:
        stats = {}
    en_cours = {k: {"stage": v.get("stage"), "depuis_s": round(time.time() - v["t0"], 1)}
                for k, v in JOBS.items() if v.get("t0") and v.get("stage") not in ("sent", "done", "error")}
    return jsonify({"objectif_s": 80, "stats": stats, "derniers_envois": list(reversed(LAST_SENDS)),
                    "en_cours": en_cours}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
