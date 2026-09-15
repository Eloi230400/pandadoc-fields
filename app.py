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
import os, io, json, time, threading, traceback, requests, fitz
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
        params = {}
        if fields:
            params = [("fields[]", f) for f in fields]
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_VENTES}/{record_id}",
                         headers=h, params=params, timeout=15)
        if r.status_code >= 400:
            return None
        return r.json().get("fields", {})
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
            cur = airtable_get_fields(record_id, [AT_FIELD_STATUT]) or {}
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
    return dump(body), dump(annexe), sig_idx


MAX_IMG_W = 900          # largeur max des images bitmap apres reduction
JPEG_QUALITY = 80        # qualite JPEG des images recompressees
FIDELITY_DPI = 50        # resolution du controle de fidelite page a page
FIDELITY_MAX = 6.0       # ecart moyen tolere (0-255) avant retour a l'original


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


def _headers(key):
    return {"Authorization": f"API-Key {key}"}


def _wait_draft(doc_id, key, tries=50):
    for _ in range(tries):
        time.sleep(1.5)
        try:
            st = requests.get(f"{PANDADOC}/documents/{doc_id}/details",
                              headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            st = None
        if st == "document.draft":
            return True
    return False


def _wait_section(doc_id, up_id, key, tries=60):
    for _ in range(tries):
        time.sleep(1.5)
        try:
            st = requests.get(f"{PANDADOC}/documents/{doc_id}/sections/uploads/{up_id}",
                              headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            st = None
        if st and "PROCESSED" in str(st).upper():
            return True
    return False


def assemble_bg(doc_id, key, template_uuid, recipient, annexe_pdf,
                subject=None, do_send=True, message=None, fields=None,
                tokens=None, record_id=None):
    """Tache de fond : ajoute la page signature (modele) puis les annexes,
    puis ENVOIE le contrat au signataire (plus de brouillon).
    v24 : quoi qu'il arrive, le resultat reel est ecrit dans la vente Airtable."""
    job = JOBS.setdefault(doc_id, {})
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
        rr = requests.post(f"{PANDADOC}/documents/{doc_id}/sections/uploads",
                           headers={**_headers(key), "Content-Type": "application/json"},
                           data=json.dumps(sec), timeout=90)
        if rr.status_code >= 400:
            job.update(stage="error", error=f"add-signature {rr.status_code}: {rr.text[:300]}"); return
        _wait_section(doc_id, rr.json().get("uuid"), key)
        _wait_draft(doc_id, key)

        # annexes en DERNIER (section fichier)
        if annexe_pdf:
            job["stage"] = "add-annexe"
            ra = requests.post(f"{PANDADOC}/documents/{doc_id}/sections/uploads",
                               headers=_headers(key),
                               files={"file": ("annexes.pdf", annexe_pdf, "application/pdf")},
                               data={"data": json.dumps({"name": "Annexes"})}, timeout=90)
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
        sd = requests.post(f"{PANDADOC}/documents/{doc_id}/send",
                           headers={**_headers(key), "Content-Type": "application/json"},
                           data=json.dumps(send_body), timeout=90)
        if sd.status_code >= 400:
            job.update(stage="error",
                       error=f"send {sd.status_code}: {sd.text[:300]}"); return
        job["stage"] = "sent"
    except Exception as e:
        job.update(stage="error", error=str(e), trace=traceback.format_exc()[-500:])


@app.post("/create-draft")
def create_draft():
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
        try:
            pdf = requests.get(d["pdf_url"], timeout=60).content
        except Exception as e:
            return jsonify({"ok": False, "stage": "download", "error": str(e)}), 502

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

        JOBS[doc_id] = {"stage": "queued", "contract_type": ctype, "will_send": do_send,
                        "visio_choices": visio, "record_id": record_id or None,
                        "airtable_doc_id_write": (at_first[1] if at_first else "record_id absent")}
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
        return jsonify({"ok": True, "name": n, "bytes": len(c)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.post("/relay-up/<n>")
def relay_up(n):
    RELAY[n] = request.get_data()
    return jsonify({"ok": True, "name": n, "bytes": len(RELAY[n])})


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
            scopes = w.json().get("scopes") or []
            out["scopes"] = scopes
            out["peut_ecrire"] = ("data.records:write" in scopes)
        else:
            out["whoami"] = f"HTTP {w.status_code}"
    except Exception as e:
        out["whoami"] = str(e)
    try:
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_VENTES}",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"maxRecords": 1, "fields[]": AT_FIELD_DOC_ID}, timeout=15)
        out["lecture_ventes"] = "ok" if r.status_code < 400 else f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        out["lecture_ventes"] = str(e)
    out["ok"] = bool(out.get("peut_ecrire")) and out.get("lecture_ventes") == "ok"
    out["champs_attendus"] = [AT_FIELD_DOC_ID, AT_FIELD_LINK, AT_FIELD_STATUT, AT_FIELD_ERREUR]
    return jsonify(out), 200


@app.get("/")
def health():
    airtable = "oui" if os.environ.get("AIRTABLE_TOKEN") else "NON"
    mode = ("cases TOUJOURS cochees (forcees)" if FORCE_CHECKBOXES
            else f"cases pre-cochees d'apres la visio, lecture Airtable: {airtable}")
    return (f"Contrat PandaDoc service OK (async v24 - {mode} - 3 modeles signature "
            "consolides - CC closer + metadata record_id + nom signataire - retour "
            f"Airtable apres envoi reel: ID doc + lien de signature + statut, jeton Airtable: {airtable})"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
