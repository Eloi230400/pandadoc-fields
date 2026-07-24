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
    "b2b": "ALxHsnBxvnYiXYUJzfwJmX",
    "b2c": "pJJCtUi3JxVVKdGmZMwkgL",
}
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


def _headers(key):
    return {"Authorization": f"API-Key {key}"}


def _wait_draft(doc_id, key, tries=25):
    for _ in range(tries):
        time.sleep(3)
        try:
            st = requests.get(f"{PANDADOC}/documents/{doc_id}/details",
                              headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            st = None
        if st == "document.draft":
            return True
    return False


def _wait_section(doc_id, up_id, key, tries=30):
    for _ in range(tries):
        time.sleep(3)
        try:
            st = requests.get(f"{PANDADOC}/documents/{doc_id}/sections/uploads/{up_id}",
                              headers=_headers(key), timeout=30).json().get("status")
        except Exception:
            st = None
        if st and "PROCESSED" in str(st).upper():
            return True
    return False


def assemble_bg(doc_id, key, template_uuid, recipient, annexe_pdf,
                subject=None, do_send=True):
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
            "message": ("Bonjour,\n\nVoici votre contrat a signer electroniquement. "
                        "Merci de le parcourir, de cocher les 2 cases obligatoires, "
                        "puis de le signer.\n\nBien a vous,\nArthaud Immobilier Academie"),
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

        # 2) Corps (avant signature) + annexes (apres signature)
        body_pdf, annexe_pdf, sig_idx = split_body(pdf)
        if not body_pdf:
            return jsonify({"ok": False, "stage": "split", "error": "corps vide"}), 500

        # 3) Creer le document PandaDoc a partir du corps (RAPIDE)
        #    Nom du document : "Contrat – {Produit} – {Prenom Nom}" si produit /
        #    client_nom sont fournis par le Zap ; sinon document_name ; sinon defaut.
        produit = (d.get("produit") or "").strip()
        client_nom = (d.get("client_nom") or "").strip()
        if d.get("document_name"):
            doc_name = d["document_name"]
        elif produit or client_nom:
            doc_name = " – ".join(["Contrat"] + [p for p in (produit, client_nom) if p])
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
        JOBS[doc_id] = {"stage": "queued", "contract_type": ctype, "will_send": do_send}
        threading.Thread(target=assemble_bg,
                         args=(doc_id, key, template_uuid, recipient, annexe_pdf),
                         kwargs={"subject": meta["name"], "do_send": do_send},
                         daemon=True).start()

        return jsonify({"ok": True, "document_id": doc_id,
                        "status": "assembling+send" if do_send else "assembling",
                        "contract_type": ctype, "signature_page_index": sig_idx,
                        "edit_url": f"https://app.pandadoc.com/a/#/documents/{doc_id}"})
    except Exception as e:
        return jsonify({"ok": False, "stage": "unhandled",
                        "error": str(e), "trace": traceback.format_exc()[-800:]}), 500


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
    return "Contrat PandaDoc service OK (async v5 - envoi auto + nommage Contrat-Produit-Client)", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
