"""
Micro-service "Champs PandaDoc" — Arthaud Immobilier Académie
-------------------------------------------------------------
Reçoit l'URL d'un PDF PDFMonkey (contrat déjà rempli), y injecte les VRAIS
champs de formulaire (1 signature + 2 cases à cocher) aux positions marquées
par les tags, puis crée le document dans PandaDoc en tant que brouillon avec
le client positionné comme SIGNATAIRE.

Les positions sont trouvées dynamiquement à partir des marqueurs présents dans
le PDF :  [signature:client:sig]  [checkbox:client:acces]  [checkbox:client:conditions]
=> robuste même si la mise en page évolue.

ENV requis :
    PANDADOC_API_KEY   ta clé API PandaDoc (Paramètres > API et intégrations)

Endpoint :
    POST /create-draft
    Body JSON :
    {
      "pdf_url": "https://.../contrat.pdf",
      "document_name": "Contrat Mastermind — V-2026-0332",
      "client_email": "client@exemple.com",
      "client_first_name": "Julien",
      "client_last_name": "Meunier"
    }
    Réponse : { "document_id": "...", "status": "...", "edit_url": "..." }
"""
import os, io, requests, fitz  # fitz = PyMuPDF
from flask import Flask, request, jsonify

app = Flask(__name__)
PANDADOC_API = "https://api.pandadoc.com/public/v1/documents"

TAGS = {
    "[signature:client:sig]":         ("signature_client", "signature"),
    "[checkbox:client:acces]":        ("case_acces",       "checkbox"),
    "[checkbox:client:conditions]":   ("case_conditions",  "checkbox"),
}

def inject_fields(pdf_bytes: bytes) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    # 1) repérer chaque tag + poser le widget correspondant
    for page in doc:
        redact = []
        for tag, (name, kind) in TAGS.items():
            for r in page.search_for(tag):
                redact.append(r)
                w = fitz.Widget()
                w.field_name = name
                if kind == "checkbox":
                    w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
                    w.field_value = False
                    w.rect = fitz.Rect(r.x0 - 1, r.y0 - 1, r.x0 + 12, r.y0 + 12)
                    w.border_color = (0.6, 0.6, 0.6); w.fill_color = (1, 1, 1); w.border_width = 0.6
                else:  # signature
                    w.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
                    w.rect = fitz.Rect(r.x0 - 2, r.y0 - 26, r.x0 + 180, r.y0 + 4)
                page.add_widget(w)
        # 2) effacer le texte des tags (pour qu'ils ne soient plus visibles)
        for r in redact:
            page.add_redact_annot(r, fill=None)
        if redact:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    out = io.BytesIO()
    doc.save(out, garbage=3, deflate=True)
    return out.getvalue()

@app.post("/create-draft")
def create_draft():
    d = request.get_json(force=True)
    api_key = os.environ["PANDADOC_API_KEY"]

    # a) télécharger le PDF PDFMonkey
    pdf = requests.get(d["pdf_url"], timeout=60).content
    # b) injecter les champs natifs
    pdf = inject_fields(pdf)

    # c) créer le document PandaDoc depuis le fichier (multipart), champs -> rôle "client"
    import json
    meta = {
        "name": d.get("document_name", "Contrat Mastermind"),
        "recipients": [{
            "email": d["client_email"],
            "first_name": d.get("client_first_name", ""),
            "last_name": d.get("client_last_name", ""),
            "role": "client",
        }],
        "parse_form_fields": True,
        "fields": {
            "signature_client": {"role": "client"},
            "case_acces":       {"role": "client"},
            "case_conditions":  {"role": "client"},
        },
    }
    r = requests.post(
        PANDADOC_API,
        headers={"Authorization": f"API-Key {api_key}"},
        files={"file": ("contrat.pdf", pdf, "application/pdf")},
        data={"data": json.dumps(meta)},
        timeout=90,
    )
    r.raise_for_status()
    doc = r.json()
    return jsonify({
        "document_id": doc.get("id"),
        "status": doc.get("status"),
        "edit_url": f"https://app.pandadoc.com/a/#/documents/{doc.get('id')}",
    })

@app.get("/")
def health():
    return "PandaDoc fields service OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
