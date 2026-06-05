from flask import Flask, render_template, request, jsonify, Response, session
from openai import OpenAI
from dotenv import load_dotenv
import json, re, queue, threading, uuid, os
from datetime import datetime, date, timedelta

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "chartpilot-dev-key-change-in-prod")

client = OpenAI()

# ── SSE ────────────────────────────────────────────────────────────────────────
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()

def _broadcast(payload: dict):
    data = json.dumps(payload)
    with _clients_lock:
        for q in list(_clients):
            try: q.put_nowait(data)
            except queue.Full: pass

def sse_stream():
    q: queue.Queue = queue.Queue(maxsize=30)
    with _clients_lock:
        _clients.append(q)
    try:
        # Send full current state on connect so reconnecting/late-joining clients
        # always get the latest note — whether or not a patient queue entry exists.
        active = get_active_encounter()
        yield f"data: {json.dumps({'type':'queue','data':get_queue_payload()})}\n\n"
        if active:
            yield f"data: {json.dumps({'type':'patient','data':safe_encounter(active)})}\n\n"
            yield f"data: {json.dumps({'type':'intake','data':active['intake_note']})}\n\n"
            yield f"data: {json.dumps({'type':'scribe','data':active['scribe_note']})}\n\n"
        else:
            # No patient queued — still send last-generated notes so display is never blank
            yield f"data: {json.dumps({'type':'intake','data':_last_intake})}\n\n"
            yield f"data: {json.dumps({'type':'scribe','data':_last_scribe})}\n\n"
        while True:
            try:
                data = q.get(timeout=12)
                yield f"data: {data}\n\n"
            except queue.Empty:
                yield ": ping\n\n"  # keepalive — prevents Render proxy from cutting the connection
    except GeneratorExit:
        pass
    finally:
        with _clients_lock:
            if q in _clients: _clients.remove(q)

# ── Encounter management ───────────────────────────────────────────────────────
ENCOUNTERS: dict[str, dict] = {}       # eid → encounter
ENCOUNTER_ORDER: list[str]  = []       # today's order
_active_eid: str | None = None

EMPTY_INTAKE = {"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}
EMPTY_SCRIBE = {"subjective":"","physical_exam":"","assessment":"","plan":"","patient_instructions":"","follow_up":""}

# Last-broadcast note — always current so reconnecting/late-joining display clients
# never miss an update regardless of timing or whether a patient queue is used.
_last_intake: dict = EMPTY_INTAKE.copy()
_last_scribe: dict = EMPTY_SCRIBE.copy()

STATUS_LABELS = {
    "waiting":   "Waiting",
    "intake":    "In Intake",
    "ready":     "Ready",
    "in_visit":  "In Visit",
    "complete":  "Complete",
}

def purge_old_encounters():
    """Remove encounters older than 12 hours."""
    cutoff = datetime.now() - timedelta(hours=12)
    stale = [eid for eid, e in ENCOUNTERS.items()
             if datetime.fromisoformat(e["created_at"]) < cutoff]
    for eid in stale:
        ENCOUNTERS.pop(eid, None)
        if eid in ENCOUNTER_ORDER: ENCOUNTER_ORDER.remove(eid)
    global _active_eid
    if _active_eid not in ENCOUNTERS:
        _active_eid = ENCOUNTER_ORDER[0] if ENCOUNTER_ORDER else None

def get_active_encounter() -> dict | None:
    if _active_eid and _active_eid in ENCOUNTERS:
        return ENCOUNTERS[_active_eid]
    return None

def safe_encounter(e: dict) -> dict:
    """Strip large note dicts for queue payload."""
    return {k: v for k, v in e.items() if k not in ("intake_note","scribe_note")}

def get_queue_payload() -> list:
    purge_old_encounters()
    return [safe_encounter(ENCOUNTERS[eid]) for eid in ENCOUNTER_ORDER if eid in ENCOUNTERS]

def push_note(note_type: str, note: dict, fresh: bool = False):
    """Update globals + active encounter, then broadcast to all connected display clients.
    Set fresh=True when called from generate/scribe_generate so the display plays a chime.
    """
    global _last_intake, _last_scribe
    if note_type == "intake":
        _last_intake = note
    else:
        _last_scribe = note
    active = get_active_encounter()
    if active:
        active[f"{note_type}_note"] = note
        active["updated_at"] = datetime.now().isoformat()
    _broadcast({"type": note_type, "data": note, "fresh": fresh})

def push_queue():
    _broadcast({"type": "queue", "data": get_queue_payload()})
    active = get_active_encounter()
    if active:
        _broadcast({"type": "patient", "data": safe_encounter(active)})

# ── Helpers ────────────────────────────────────────────────────────────────────
def safe_json_parse(text: str, fallback: dict | None = None) -> dict:
    try: return json.loads(text)
    except:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try: return json.loads(m.group(0))
            except: pass
    return fallback if fallback is not None else {}

def chat(system_prompt: str, user_content: str, max_tokens: int = 900) -> str:
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_content}],
        temperature=0.1, max_tokens=max_tokens,
        response_format={"type":"json_object"},
    )
    return r.choices[0].message.content.strip()

def truncate(text: str, max_chars: int = 6000) -> str:
    return text if len(text) <= max_chars else "...[earlier omitted]...\n" + text[-max_chars:]

# ── PIN auth for /display ──────────────────────────────────────────────────────
DISPLAY_PIN = os.environ.get("DISPLAY_PIN", "")  # set in Render env vars; empty = no PIN

def display_authed() -> bool:
    if not DISPLAY_PIN: return True
    return session.get("display_authed") is True

# ── Whisper prompts ────────────────────────────────────────────────────────────
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
    "Weber lateralizes, Rinne test, Dix-Hallpike positive. "
    "Impressions: allergic rhinitis, chronic sinusitis, otitis media, otitis externa, "
    "serous otitis media, eustachian tube dysfunction, BPPV, Meniere's disease, "
    "tonsillitis, peritonsillar abscess, LPR, GERD, vocal cord nodules, hearing loss. "
    "Plan: prescribe Augmentin, start prednisone taper, Ciprodex ear drops, Flonase, "
    "refer to audiology, order CT sinuses, schedule tonsillectomy, Epley maneuver, "
    "follow up in two weeks, return to clinic in one month."
)

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
ethmoid sinus, sphenoid sinus, nasopharynx, oropharynx, larynx, vocal cords, adenoids.
Procedures: myringotomy, tympanostomy tubes, cerumen removal, tympanoplasty, septoplasty,
turbinate reduction, FESS, balloon sinuplasty, tonsillectomy, adenoidectomy, laryngoscopy,
audiogram, tympanogram, Epley maneuver, CT sinus, MRI, fine needle aspiration.
"""

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    return jsonify({"ok": True})

@app.route("/api/notes")
def api_notes():
    """Polling fallback — returns the latest generated notes.
    Used by the display page when SSE drops or server restarts."""
    active = get_active_encounter()
    if active:
        return jsonify({
            "intake": active["intake_note"],
            "scribe": active["scribe_note"],
            "patient": safe_encounter(active),
        })
    return jsonify({
        "intake": _last_intake,
        "scribe": _last_scribe,
        "patient": None,
    })

@app.route("/")
def home(): return render_template("index.html")

@app.route("/scribe")
def scribe(): return render_template("scribe.html")

@app.route("/display")
def display():
    if not display_authed():
        return render_template("display.html", pin_required=True, pin_set=bool(DISPLAY_PIN))
    return render_template("display.html", pin_required=False, pin_set=bool(DISPLAY_PIN))

@app.route("/display/auth", methods=["POST"])
def display_auth():
    pin = request.json.get("pin","")
    if pin == DISPLAY_PIN:
        session["display_authed"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect PIN"}), 401

@app.route("/stream")
def stream():
    return Response(sse_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── Patient / Encounter API ────────────────────────────────────────────────────
@app.route("/api/encounters", methods=["GET"])
def list_encounters():
    purge_old_encounters()
    return jsonify(get_queue_payload())

@app.route("/api/encounters", methods=["POST"])
def add_encounter():
    global _active_eid
    data = request.json or {}
    mrn = (data.get("mrn") or "").strip()
    # If no MRN provided, auto-generate a visit label from room or sequence
    if not mrn:
        room_hint = (data.get("room") or "").strip()
        visit_num = len(ENCOUNTER_ORDER) + 1
        mrn = room_hint if room_hint else f"Visit {visit_num}"

    eid = uuid.uuid4().hex[:8].upper()
    enc = {
        "id":                eid,
        "mrn":               mrn,
        "initials":          (data.get("initials") or "").strip().upper()[:4],
        "room":              (data.get("room") or "").strip(),
        "provider":          (data.get("provider") or "").strip(),
        "status":            "waiting",
        "date":              date.today().isoformat(),
        "created_at":        datetime.now().isoformat(),
        "updated_at":        datetime.now().isoformat(),
        "intake_note":  EMPTY_INTAKE.copy(),
        "scribe_note":  EMPTY_SCRIBE.copy(),
    }
    ENCOUNTERS[eid] = enc
    ENCOUNTER_ORDER.append(eid)

    # Auto-select if no active encounter
    if _active_eid is None:
        _active_eid = eid

    push_queue()
    return jsonify(safe_encounter(enc))

@app.route("/api/encounters/<eid>", methods=["GET"])
def get_encounter(eid):
    if eid not in ENCOUNTERS:
        return jsonify({"error": "Not found"}), 404
    return jsonify(ENCOUNTERS[eid])

@app.route("/api/encounters/<eid>/select", methods=["POST"])
def select_encounter(eid):
    global _active_eid
    if eid not in ENCOUNTERS:
        return jsonify({"error": "Not found"}), 404
    _active_eid = eid
    push_queue()
    active = ENCOUNTERS[eid]
    _broadcast({"type": "patient", "data": safe_encounter(active)})
    _broadcast({"type": "intake",  "data": active["intake_note"]})
    _broadcast({"type": "scribe",  "data": active["scribe_note"]})
    return jsonify({"ok": True})

@app.route("/api/encounters/<eid>/status", methods=["POST"])
def update_status(eid):
    if eid not in ENCOUNTERS:
        return jsonify({"error": "Not found"}), 404
    new_status = (request.json or {}).get("status","")
    if new_status not in STATUS_LABELS:
        return jsonify({"error": "Invalid status"}), 400
    ENCOUNTERS[eid]["status"] = new_status
    ENCOUNTERS[eid]["updated_at"] = datetime.now().isoformat()
    push_queue()
    return jsonify({"ok": True})

@app.route("/api/encounters/<eid>", methods=["DELETE"])
def delete_encounter(eid):
    global _active_eid
    ENCOUNTERS.pop(eid, None)
    if eid in ENCOUNTER_ORDER: ENCOUNTER_ORDER.remove(eid)
    if _active_eid == eid:
        _active_eid = ENCOUNTER_ORDER[0] if ENCOUNTER_ORDER else None
    push_queue()
    return jsonify({"ok": True})

# ── Intake ─────────────────────────────────────────────────────────────────────
@app.route("/transcribe", methods=["POST"])
def transcribe():
    try:
        t = client.audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", request.files["audio"].stream, "audio/webm"),
            language="en", temperature=0, prompt=INTAKE_WHISPER_PROMPT,
        )
        # Auto-update status to "intake" when recording starts
        active = get_active_encounter()
        if active and active["status"] == "waiting":
            active["status"] = "intake"
            active["updated_at"] = datetime.now().isoformat()
            push_queue()
        return jsonify({"transcript": t.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/live_update", methods=["POST"])
def live_update():
    try:
        transcript = truncate(request.json.get("transcript",""))
        current = request.json.get("current_note", EMPTY_INTAKE.copy())
        result = chat(
            f"""Real-time ENT intake note assistant. ENT vocab:\n{ENT_VOCAB}
Rules: extract only stated info. Never diagnose. Preserve good existing content.
If not discussed: "Not discussed." Medications: all Rx/OTC/drops mentioned; if denied: "Patient denies."
Allergies: name + reaction. Handoff: 2-4 sentence clinical summary. JSON only.""",
            f"Current note:\n{json.dumps(current,indent=2)}\n\nTranscript:\n{transcript}\n\n"
            f'Return: {{"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}}',
            max_tokens=600,
        )
        note = safe_json_parse(result, current)
        push_note("intake", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate():
    try:
        transcript = truncate(request.json.get("transcript",""))
        result = chat(
            f"""ENT intake assistant generating final physician-review note. ENT vocab:\n{ENT_VOCAB}
Rules: only stated info. "Not discussed" if absent. No diagnosis/treatment suggestions.
HPI: narrative (onset, duration, character, severity). ROS: bullets of discussed items.
Medications: all Rx/OTC/drops; if denied: "Patient denies." Allergies: name + reaction type.
Handoff: 3-5 sentence high-yield clinical summary. JSON only.""",
            f"Transcript:\n{transcript}\n\n"
            f'Return: {{"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}}',
            max_tokens=900,
        )
        note = safe_json_parse(result)
        push_note("intake", note, fresh=True)
        # Auto-advance status to "ready"
        active = get_active_encounter()
        if active and active["status"] in ("waiting","intake"):
            active["status"] = "ready"
            active["updated_at"] = datetime.now().isoformat()
            push_queue()
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/clear_note", methods=["POST"])
def clear_note():
    global _last_intake, _last_scribe
    _last_intake = EMPTY_INTAKE.copy()
    _last_scribe = EMPTY_SCRIBE.copy()
    push_note("intake", EMPTY_INTAKE.copy())
    push_note("scribe", EMPTY_SCRIBE.copy())
    return jsonify({"ok": True})

# ── Scribe ─────────────────────────────────────────────────────────────────────
@app.route("/scribe_transcribe", methods=["POST"])
def scribe_transcribe():
    try:
        t = client.audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", request.files["audio"].stream, "audio/webm"),
            language="en", temperature=0, prompt=SCRIBE_WHISPER_PROMPT,
        )
        active = get_active_encounter()
        if active and active["status"] == "ready":
            active["status"] = "in_visit"
            active["updated_at"] = datetime.now().isoformat()
            push_queue()
        return jsonify({"transcript": t.text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_live", methods=["POST"])
def scribe_live():
    try:
        transcript = truncate(request.json.get("transcript",""))
        current = request.json.get("current_note", EMPTY_SCRIBE.copy())
        result = chat(
            f"""Real-time AI medical scribe for ENT physician. ENT vocab:\n{ENT_VOCAB}
Transcribing live doctor-patient encounter.
Subjective: patient's symptoms/history this visit.
Physical Exam: exam findings the DOCTOR states.
Assessment: diagnoses/impressions the DOCTOR states.
Plan: treatments/prescriptions/referrals the DOCTOR mentions.
Patient Instructions: what doctor TELLS patient to do.
Follow-up: when doctor asks patient to return.
Only capture what is explicitly said. If not yet discussed: "Not yet documented."
Preserve good existing content. JSON only.""",
            f"Current note:\n{json.dumps(current,indent=2)}\n\nLive transcript:\n{transcript}\n\n"
            f'Return: {{"subjective":"","physical_exam":"","assessment":"","plan":"","patient_instructions":"","follow_up":""}}',
            max_tokens=700,
        )
        note = safe_json_parse(result, current)
        push_note("scribe", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_generate", methods=["POST"])
def scribe_generate():
    try:
        transcript = truncate(request.json.get("transcript",""))
        result = chat(
            f"""AI medical scribe for ENT physician, final clinical encounter note. ENT vocab:\n{ENT_VOCAB}
Subjective: patient's reported symptoms/history during visit.
Physical Exam: ALL exam findings doctor stated — specific and clinical.
Assessment: ALL diagnoses/impressions doctor stated.
Plan: ALL treatments, prescriptions with doses if mentioned, referrals, imaging.
Patient Instructions: everything doctor told patient to do — specific dosing, restrictions.
Follow-up: exact return timeframe or referral instructions.
Only stated info. "Not documented" if absent. No inferred diagnoses. Clinical third-person prose. JSON only.""",
            f"Full transcript:\n{transcript}\n\n"
            f'Return: {{"subjective":"","physical_exam":"","assessment":"","plan":"","patient_instructions":"","follow_up":""}}',
            max_tokens=1100,
        )
        note = safe_json_parse(result)
        push_note("scribe", note, fresh=True)
        active = get_active_encounter()
        if active and active["status"] == "in_visit":
            active["status"] = "complete"
            active["updated_at"] = datetime.now().isoformat()
            push_queue()
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_clear", methods=["POST"])
def scribe_clear():
    push_note("scribe", EMPTY_SCRIBE.copy())
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
