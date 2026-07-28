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

# Modeles PandaDoc "page signature" (crees dans l'editeur, cases = OBLIGATOIRES)
TEMPLATES = {
    "b2b": "u8jMx9jGwNXFXUK4JDH8uN",              # Mastermind B2B v2 (libelles corriges)
    "b2c": "9F7HwBwA7QUjhWCUUAn8aj",              # Mastermind B2C v2 (libelles corriges)
    "b2b:incubateur": "EZgpsbDUidtEEpBTQJaQxL",   # Incubateur B2B v4 (nom client + date, sans encadre)
    "b2c:incubateur": "r85DmX64qKQfFTTtZvsuq9",   # Incubateur B2C v4 (nom client + date, sans encadre)
    "b2b:formation": "spoAWUWuysMp4oKckbu6LC",    # Formation classique B2B v1
    "b2c:formation": "MKK6TUSLdmjShEe6tL2CjC",    # Formation classique B2C v1
    "b2b:starter": "iREzxvQhESHQ6DTtgKTceF",      # Formation Starter B2B v1
    "b2c:starter": "7NDpbrqUrSNEnSywJgQxWd",      # Formation Starter B2C v1
    "b2b:elite": "mcPnAf4rDy9KYvADAjGCNX",   # Mastermind Elite B2B
    "b2c:elite": "j3KQxcwb2u7ynDKtYfCik2",   # Mastermind Elite B2C
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
                subject=None, do_send=True, message=None, fields=None):
    """Tache de fond : ajoute la page signature (modele) puis les annexes,
    puis ENVOIE le contrat au signataire (plus de brouillon)."""
    job = JOBS.setdefault(doc_id, {})
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
        meta = {"name": doc_name,
                "recipients": [dict(recipient, role="client")]}
        r = requests.post(f"{PANDADOC}/documents", headers=_headers(key),
                          files={"file": ("corps.pdf", body_pdf, "application/pdf")},
                          data={"data": json.dumps(meta)}, timeout=90)
        if r.status_code >= 400:
            return jsonify({"ok": False, "stage": "create-body",
                            "http_status": r.status_code, "error": r.text[:800]}), 502
        doc_id = r.json()["id"]

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

        JOBS[doc_id] = {"stage": "queued", "contract_type": ctype, "will_send": do_send}
        threading.Thread(target=assemble_bg,
                         args=(doc_id, key, template_uuid, recipient, annexe_pdf),
                         kwargs={"subject": meta["name"], "do_send": do_send,
                                 "message": email_message,
                                 "fields": (prefill or None)},
                         daemon=True).start()

        return jsonify({"ok": True, "document_id": doc_id,
                        "status": "assembling+send" if do_send else "assembling",
                        "contract_type": ctype, "signature_page_index": sig_idx,
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


@app.get("/")
def health():
    return "Contrat PandaDoc service OK (async v17 - Mastermind + Incubateur + Formation + Starter (B2B/B2C))", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
