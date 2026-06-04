from flask import Flask, render_template, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
import json
import re

load_dotenv()

app = Flask(__name__)
client = OpenAI()

ENT_VOCAB = """
mometasone, azelastine, fluticasone, Flonase, Nasacort, Rhinocort, Nasonex,
Dymista, Astepro, ipratropium nasal spray, Atrovent nasal spray, Afrin,
oxymetazoline, saline spray, saline rinse, NeilMed, Xhance, Zyrtec, cetirizine,
Claritin, loratadine, Allegra, fexofenadine, Xyzal, levocetirizine, Benadryl,
diphenhydramine, Singulair, montelukast, Sudafed, pseudoephedrine, Mucinex,
guaifenesin, amoxicillin, Augmentin, amoxicillin clavulanate, cefdinir, Omnicef,
azithromycin, Z-Pak, doxycycline, clindamycin, Bactrim, sulfamethoxazole
trimethoprim, Levaquin, levofloxacin, ciprofloxacin, Cipro, prednisone,
Medrol Dosepak, methylprednisolone, dexamethasone, Decadron, Kenalog,
triamcinolone, ofloxacin, Floxin, Ciprodex, ciprofloxacin dexamethasone,
Cortisporin, neomycin polymyxin hydrocortisone, Debrox, carbamide peroxide,
acetic acid drops, Vosol, fluocinolone oil, DermOtic, omeprazole, Prilosec,
pantoprazole, Protonix, famotidine, Pepcid, Nexium, esomeprazole, lansoprazole,
Prevacid, Gaviscon, Tums, meclizine, Antivert, ondansetron, Zofran,
promethazine, Phenergan, scopolamine patch, diazepam, Valium, Tylenol,
acetaminophen, Advil, ibuprofen, Motrin, Aleve, naproxen, aspirin,
otalgia, otorrhea, tinnitus, vertigo, rhinorrhea, postnasal drip, dysphagia,
odynophagia, dysphonia, globus sensation, epistaxis, anosmia, hyposmia,
tympanic membrane, eardrum, Eustachian tube, nasal septum, turbinates,
maxillary sinus, frontal sinus, ethmoid sinus, sphenoid sinus, larynx,
vocal cords, nasopharynx, oropharynx, hypopharynx, audiogram, tympanogram,
tympanometry, nasal endoscopy, laryngoscopy, CT sinus, myringotomy,
tympanostomy tube, cerumen removal, septoplasty, turbinate reduction,
tonsillectomy, adenoidectomy, balloon sinuplasty
"""

EMPTY_NOTE = {
    "chief_complaint": "",
    "hpi": "",
    "ros": "",
    "medications": "",
    "allergies": "",
    "doctor_handoff": ""
}

def safe_json_parse(text, fallback=None):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return fallback or EMPTY_NOTE

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/transcribe", methods=["POST"])
def transcribe():
    audio_file = request.files["audio"]

    transcription = client.audio.transcriptions.create(
        model="whisper-1",
        file=("recording.webm", audio_file.stream, "audio/webm")
    )

    raw_text = transcription.text.strip()

    correction = client.responses.create(
        model="gpt-4.1-mini",
        input=f"""
You are correcting a speech-to-text transcript from an ENT clinic intake.

Use this ENT vocabulary bank to correct obvious transcription mistakes:

{ENT_VOCAB}

Examples:
- "my metazone is the last team" likely means "mometasone azelastine"
- "flow nays" likely means "Flonase"
- "sir tech" likely means "Zyrtec"
- "augmenton" likely means "Augmentin"

Rules:
- Correct obvious speech-to-text errors.
- Do not add facts.
- Do not summarize.
- Return only the corrected transcript.

Transcript:
{raw_text}
"""
    )

    return jsonify({"transcript": correction.output_text.strip()})

@app.route("/live_update", methods=["POST"])
def live_update():
    transcript = request.json.get("transcript", "")
    current_note = request.json.get("current_note", EMPTY_NOTE)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=f"""
You are updating an ENT intake note in real time.

Rules:
- Only use stated information.
- Do not diagnose.
- Do not create a treatment plan.
- Preserve useful existing information.
- If unknown, write "Not discussed."
- Return JSON only.

Medication rules:
- Include prescription meds, OTC meds, nasal sprays, allergy meds, antibiotics, steroids, and ear drops.
- If denied, write "Patient denies current medications."
- If not discussed, write "Not discussed."

Current note:
{json.dumps(current_note)}

Running transcript:
{transcript}

Return exactly:
{{
  "chief_complaint": "",
  "hpi": "",
  "ros": "",
  "medications": "",
  "allergies": "",
  "doctor_handoff": ""
}}
"""
    )

    return jsonify(safe_json_parse(response.output_text.strip(), current_note))

@app.route("/generate", methods=["POST"])
def generate():
    transcript = request.json.get("transcript", "")

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=f"""
You are an ENT medical intake assistant creating a final doctor review note.

Rules:
- Only use information actually stated.
- If not discussed, write "Not discussed."
- Do not diagnose.
- Do not create a treatment plan.
- Make the final note clean and concise.
- Return JSON only.

Medication rules:
- Include prescription meds, OTC meds, nasal sprays, allergy meds, antibiotics, steroids, and ear drops.
- If denied, write "Patient denies current medications."
- If not discussed, write "Not discussed."

Return exactly:
{{
  "chief_complaint": "",
  "hpi": "",
  "ros": "",
  "medications": "",
  "allergies": "",
  "doctor_handoff": ""
}}

Transcript:
{transcript}
"""
    )

    return jsonify(safe_json_parse(response.output_text.strip()))

if __name__ == "__main__":
    app.run(debug=True)