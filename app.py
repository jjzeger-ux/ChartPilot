from flask import Flask, render_template, request, jsonify, Response
from openai import OpenAI
from dotenv import load_dotenv
import json
import re
import queue
import threading

load_dotenv()

app = Flask(__name__)
client = OpenAI()

# ── SSE state ──────────────────────────────────────────────────────────────────
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()

EMPTY_INTAKE = {
    "chief_complaint": "", "hpi": "", "ros": "",
    "medications": "", "allergies": "", "doctor_handoff": ""
}
EMPTY_SCRIBE = {
    "subjective": "", "physical_exam": "", "assessment": "",
    "plan": "", "patient_instructions": "", "follow_up": ""
}

current_intake: dict = EMPTY_INTAKE.copy()
current_scribe: dict = EMPTY_SCRIBE.copy()

def push_update(note_type: str, note: dict):
    """Broadcast a note update to all display clients."""
    global current_intake, current_scribe
    if note_type == "intake":
        current_intake = note
    else:
        current_scribe = note
    payload = json.dumps({"type": note_type, "data": note})
    with _clients_lock:
        for q in list(_clients):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

def sse_stream():
    q: queue.Queue = queue.Queue(maxsize=30)
    with _clients_lock:
        _clients.append(q)
    try:
        # Send current state of both notes on connect
        yield f"data: {json.dumps({'type':'intake','data':current_intake})}\n\n"
        yield f"data: {json.dumps({'type':'scribe','data':current_scribe})}\n\n"
        while True:
            try:
                data = q.get(timeout=25)
                yield f"data: {data}\n\n"
            except queue.Empty:
                yield ": ping\n\n"
    except GeneratorExit:
        pass
    finally:
        with _clients_lock:
            if q in _clients:
                _clients.remove(q)

# ── Shared helpers ─────────────────────────────────────────────────────────────
def safe_json_parse(text: str, fallback: dict | None = None) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return fallback if fallback is not None else {}

def chat(system_prompt: str, user_content: str, max_tokens: int = 900) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content.strip()

def truncate(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    return "...[earlier content omitted]...\n" + text[-max_chars:]

# ── Whisper vocabulary prompts ─────────────────────────────────────────────────
# Intake prompt: patient describing symptoms to the MA
INTAKE_WHISPER_PROMPT = (
    "ENT patient intake. Chief complaint: bilateral otalgia, otorrhea, and tinnitus. "
    "Medications: Flonase fluticasone, Nasacort mometasone, Nasonex, Rhinocort budesonide, "
    "Dymista azelastine-fluticasone, Astepro, Afrin oxymetazoline, NeilMed saline rinse, "
    "Zyrtec cetirizine, Claritin loratadine, Allegra fexofenadine, Xyzal levocetirizine, "
    "Benadryl diphenhydramine, Singulair montelukast, Sudafed pseudoephedrine, Mucinex, "
    "Augmentin amoxicillin-clavulanate, cefdinir Omnicef, azithromycin Z-Pak, doxycycline, "
    "clindamycin, Bactrim, Levaquin levofloxacin, Cipro ciprofloxacin, prednisone, "
    "Medrol Dosepak, dexamethasone Decadron, Ciprodex, Cortisporin, Debrox carbamide peroxide, "
    "Vosol acetic acid, DermOtic fluocinolone, omeprazole Prilosec, pantoprazole Protonix, "
    "famotidine Pepcid, Nexium, meclizine Antivert, ondansetron Zofran, valacyclovir Valtrex, "
    "gabapentin, nortriptyline, hydroxyzine Atarax, betahistine. "
    "Allergies: penicillin anaphylaxis, sulfa drugs, codeine. "
    "Symptoms: rhinorrhea, postnasal drip, nasal congestion, epistaxis, anosmia, hyposmia, "
    "dysphagia, odynophagia, dysphonia, globus sensation, hoarseness, vertigo, BPPV, "
    "tinnitus, aural fullness, hearing loss, eustachian tube dysfunction, "
    "snoring, sleep apnea, CPAP, LPR, laryngopharyngeal reflux, GERD. "
    "Anatomy: tympanic membrane, ossicles, cochlea, semicircular canals, mastoid, "
    "turbinates, nasal septum, maxillary sinus, frontal sinus, ethmoid sinus, sphenoid sinus, "
    "nasopharynx, oropharynx, larynx, epiglottis, vocal cords, adenoids, palatine tonsils. "
    "Procedures: myringotomy, tympanostomy tubes, septoplasty, turbinate reduction, "
    "FESS, balloon sinuplasty, tonsillectomy, adenoidectomy, laryngoscopy, audiogram."
)

# Scribe prompt: doctor-patient conversation in the exam room
SCRIBE_WHISPER_PROMPT = (
    "ENT clinical encounter. Doctor examining patient. "
    "Exam findings: tympanic membrane intact, normal light reflex, dull tympanic membrane, "
    "tympanic membrane erythematous, middle ear effusion, tympanic membrane perforation, "
    "cholesteatoma, external auditory canal, cerumen impaction, "
    "nasal mucosa erythematous, turbinates edematous, turbinate hypertrophy, "
    "nasal polyps, deviated septum, purulent discharge, clear rhinorrhea, "
    "oropharynx clear, tonsils normal, tonsils two-plus, tonsillar exudate, "
    "posterior pharyngeal wall cobblestoning, uvula midline, "
    "larynx visualized, vocal cords normal, vocal cord nodule, vocal cord polyp, "
    "neck supple, no lymphadenopathy, no palpable masses, thyroid normal, "
    "Weber lateralizes, Rinne test, Dix-Hallpike positive, positive Dix-Hallpike. "
    "Impressions: allergic rhinitis, chronic sinusitis, otitis media, otitis externa, "
    "serous otitis media, eustachian tube dysfunction, BPPV, Meniere's disease, "
    "tonsillitis, peritonsillar abscess, LPR, GERD, vocal cord nodules, hearing loss, "
    "sensorineural hearing loss, conductive hearing loss. "
    "Plan: prescribe Augmentin, start prednisone taper, Ciprodex ear drops, Flonase, "
    "refer to audiology, order CT sinuses, schedule tonsillectomy, Epley maneuver, "
    "follow up in two weeks, return to clinic in one month, "
    "finish the entire course, take twice daily with food, "
    "saline nasal rinse twice a day, avoid nose blowing, keep ear dry."
)

# ── ENT vocabulary for LLM prompts ────────────────────────────────────────────
ENT_VOCAB = """
Medications: Flonase, Nasacort, Nasonex, Rhinocort, Dymista, Astepro, Afrin, NeilMed,
Zyrtec (cetirizine), Claritin (loratadine), Allegra (fexofenadine), Xyzal, Benadryl,
Singulair (montelukast), Sudafed, Mucinex, amoxicillin, Augmentin, cefdinir (Omnicef),
azithromycin (Z-Pak), doxycycline, clindamycin, Bactrim, Levaquin, Cipro, prednisone,
Medrol Dosepak, dexamethasone (Decadron), Kenalog (triamcinolone), Ciprodex,
Cortisporin, Debrox, Vosol, DermOtic, omeprazole (Prilosec), pantoprazole (Protonix),
famotidine (Pepcid), Nexium, meclizine (Antivert), ondansetron (Zofran), valacyclovir,
gabapentin, nortriptyline, hydroxyzine, betahistine.

Conditions: otitis media, serous otitis media, otitis externa, cholesteatoma, BPPV,
Meniere's disease, tinnitus, hearing loss, sensorineural hearing loss, conductive hearing loss,
eustachian tube dysfunction, allergic rhinitis, chronic sinusitis, nasal polyposis,
deviated septum, LPR, GERD, tonsillitis, peritonsillar abscess, vocal cord nodules,
sleep apnea, acoustic neuroma, vestibular neuritis.

Anatomy: tympanic membrane, ossicles, malleus, incus, stapes, cochlea, semicircular canals,
Eustachian tube, mastoid, turbinates, nasal septum, maxillary sinus, frontal sinus,
ethmoid sinus, sphenoid sinus, nasopharynx, oropharynx, larynx, vocal cords, adenoids,
palatine tonsils, parotid, submandibular gland.

Procedures: myringotomy, tympanostomy tubes, cerumen removal, tympanoplasty, septoplasty,
turbinate reduction, FESS, balloon sinuplasty, tonsillectomy, adenoidectomy, laryngoscopy,
audiogram, tympanogram, Epley maneuver, CT sinus, MRI, fine needle aspiration.
"""

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/scribe")
def scribe():
    return render_template("scribe.html")

@app.route("/display")
def display():
    return render_template("display.html")

@app.route("/stream")
def stream():
    return Response(
        sse_stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── INTAKE transcription ───────────────────────────────────────────────────────
@app.route("/transcribe", methods=["POST"])
def transcribe():
    try:
        audio_file = request.files["audio"]
        t = client.audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", audio_file.stream, "audio/webm"),
            language="en",
            temperature=0,
            prompt=INTAKE_WHISPER_PROMPT,
        )
        return jsonify({"transcript": t.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/live_update", methods=["POST"])
def live_update():
    try:
        transcript = truncate(request.json.get("transcript", ""))
        current = request.json.get("current_note", EMPTY_INTAKE.copy())
        result = chat(
            system_prompt=f"""You are a real-time ENT intake note assistant. ENT vocabulary reference:
{ENT_VOCAB}

Rules:
- Extract only information explicitly stated.
- Never diagnose or suggest treatment.
- Preserve good existing content — only overwrite if the new transcript clearly improves a field.
- If a topic was not mentioned, write "Not discussed."
- Medications: all Rx, OTC, nasal sprays, supplements mentioned. If denied: "Patient denies current medications."
- Allergies: medication name AND reaction type.
- Doctor Handoff: 2-4 sentence clinical summary of top concerns for the physician.
- Return JSON only.""",
            user_content=f"""Current note:\n{json.dumps(current, indent=2)}\n\nRunning transcript:\n{transcript}\n\nReturn:\n{{"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}}""",
            max_tokens=600,
        )
        note = safe_json_parse(result, current)
        push_update("intake", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate():
    try:
        transcript = truncate(request.json.get("transcript", ""))
        result = chat(
            system_prompt=f"""You are an ENT medical intake assistant generating a final physician-review note. ENT vocabulary:
{ENT_VOCAB}

Rules:
- Only use information explicitly stated.
- If not discussed, write "Not discussed."
- Do not diagnose. Do not create a treatment plan.
- HPI: concise narrative — onset, duration, character, severity, modifying factors.
- ROS: bullet relevant positives and negatives actually discussed.
- Medications: all Rx, OTC, nasal sprays, ear drops mentioned. If denied: "Patient denies current medications."
- Allergies: name AND reaction type.
- Doctor Handoff: 3-5 sentence high-yield clinical summary.
- Return JSON only.""",
            user_content=f"""Transcript:\n{transcript}\n\nReturn:\n{{"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}}""",
            max_tokens=900,
        )
        note = safe_json_parse(result)
        push_update("intake", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/clear_note", methods=["POST"])
def clear_note():
    push_update("intake", EMPTY_INTAKE.copy())
    return jsonify({"ok": True})

# ── SCRIBE transcription ───────────────────────────────────────────────────────
@app.route("/scribe_transcribe", methods=["POST"])
def scribe_transcribe():
    try:
        audio_file = request.files["audio"]
        t = client.audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", audio_file.stream, "audio/webm"),
            language="en",
            temperature=0,
            prompt=SCRIBE_WHISPER_PROMPT,
        )
        return jsonify({"transcript": t.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_live", methods=["POST"])
def scribe_live():
    try:
        transcript = truncate(request.json.get("transcript", ""))
        current = request.json.get("current_note", EMPTY_SCRIBE.copy())
        result = chat(
            system_prompt=f"""You are a real-time AI medical scribe for an ENT physician. ENT vocabulary:
{ENT_VOCAB}

You are transcribing a live doctor-patient encounter. Both doctor and patient are speaking.

Field rules:
- Subjective: what the PATIENT says about their symptoms and history during this visit.
- Physical Exam: exam findings the DOCTOR states (e.g., "tympanic membrane is intact", "I see fluid behind the eardrum", "turbinates are swollen").
- Assessment: diagnoses or impressions the DOCTOR states (e.g., "this looks like otitis media", "I think you have allergic rhinitis").
- Plan: treatments, medications, referrals, or orders the DOCTOR mentions prescribing/ordering.
- Patient Instructions: what the doctor TELLS the patient to do (dosing, activity restrictions, wound care, etc.).
- Follow-up: when the doctor asks the patient to return or who to see next.

Additional rules:
- Only capture what is explicitly said — do not infer diagnoses or treatments.
- If a field topic was not discussed yet, write "Not yet documented."
- Preserve good existing content — overwrite only if new transcript clearly improves it.
- Return JSON only.""",
            user_content=f"""Current note:\n{json.dumps(current, indent=2)}\n\nLive transcript:\n{transcript}\n\nReturn:\n{{"subjective":"","physical_exam":"","assessment":"","plan":"","patient_instructions":"","follow_up":""}}""",
            max_tokens=700,
        )
        note = safe_json_parse(result, current)
        push_update("scribe", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_generate", methods=["POST"])
def scribe_generate():
    try:
        transcript = truncate(request.json.get("transcript", ""))
        result = chat(
            system_prompt=f"""You are an AI medical scribe for an ENT physician generating a final clinical encounter note. ENT vocabulary:
{ENT_VOCAB}

You are transcribing a doctor-patient encounter. Both voices are present.

Field rules:
- Subjective: the patient's reported symptoms, history, and concerns stated during this visit.
- Physical Exam: ALL exam findings the doctor stated — be specific and clinical (e.g., "Right TM: dull, retracted with effusion. Left TM: intact. Nasal mucosa: erythematous with edematous turbinates bilaterally.").
- Assessment: ALL diagnoses or impressions the doctor stated — use standard clinical terminology.
- Plan: ALL treatments, prescriptions with doses if mentioned, referrals, imaging ordered.
- Patient Instructions: everything the doctor told the patient to do — specific dosing, restrictions, wound care.
- Follow-up: exact return timeframe or referral instructions stated.

Additional rules:
- Only use information explicitly stated in the transcript.
- If a field was not discussed, write "Not documented."
- Do not infer diagnoses or treatment — only capture what was said.
- Write in concise, clinical third-person prose.
- Return JSON only.""",
            user_content=f"""Full encounter transcript:\n{transcript}\n\nReturn:\n{{"subjective":"","physical_exam":"","assessment":"","plan":"","patient_instructions":"","follow_up":""}}""",
            max_tokens=1100,
        )
        note = safe_json_parse(result)
        push_update("scribe", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_clear", methods=["POST"])
def scribe_clear():
    push_update("scribe", EMPTY_SCRIBE.copy())
    return jsonify({"ok": True})

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
