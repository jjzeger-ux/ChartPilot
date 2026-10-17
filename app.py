from flask import Flask, render_template, request, jsonify, Response, session, g, redirect, url_for, abort
from openai import OpenAI
from dotenv import load_dotenv
import json, re, uuid, os, hashlib, time
from pathlib import Path
from storage import WorkspaceStore
import auth
from datetime import datetime, date, timedelta

load_dotenv()

app = Flask(__name__)
app.secret_key = auth.secret_key()
app.permanent_session_lifetime = timedelta(hours=12)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  SESSION_COOKIE_SECURE=auth.hosted(), MAX_CONTENT_LENGTH=4_000_000,
                  TEMPLATES_AUTO_RELOAD=not auth.hosted(),
                  DATABASE_PATH=str(Path(__file__).parent / ".instance" / "hermes.sqlite"))
client = OpenAI() if os.environ.get("OPENAI_API_KEY") else None
SITE_PIN = os.environ.get("SITE_PIN", "24601")
AI_PATHS = {"/transcribe", "/live_update", "/generate", "/scribe_transcribe", "/scribe_live", "/scribe_generate"}

@app.before_request
def prepare_request():
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and origin.rstrip("/") != request.host_url.rstrip("/")):
            return jsonify(error="Request origin is not allowed."), 403
    if request.path.startswith("/static/") or request.path in ("/", "/sign-in", "/unlock", "/ping", "/favicon.ico"):
        return
    if not auth.configured():
        return jsonify(error="The hosted workspace is awaiting configuration."), 503
    if not session.get("site_authed"):
        if request.path.startswith("/api/") or request.path in AI_PATHS:
            return jsonify(error="Please sign in again."), 401
        return redirect(url_for("sign_in"))
    if request.path in AI_PATHS and client is None:
        return jsonify(error="AI is not connected yet. Set OPENAI_API_KEY on the server and restart Hermes."), 503
    if request.path == "/logout": return
    try:
        g.store = WorkspaceStore(app.config["DATABASE_PATH"], session.get("user_id", "local"),
                                 request.method not in ("GET", "HEAD", "OPTIONS"))
        g.state = g.store.state
        eid = request.headers.get("X-Hermes-Encounter")
        if eid and (request.path in AI_PATHS or request.path in ("/api/live_transcript", "/clear_note", "/scribe_clear")):
            if eid not in state()["encounters"]: return jsonify(error="Encounter not found."), 404
            state()["active"] = eid
            state()["transcript"] = state()["encounters"][eid].get("transcript", "")
    except Exception:
        app.logger.warning("Workspace database unavailable")
        return jsonify(error="Saved workspace is temporarily unavailable. Please try again."), 503

@app.after_request
def finish_request(response):
    if hasattr(g, "store") and response.status_code < 400:
        try: g.store.commit()
        except Exception:
            app.logger.warning("Workspace save failed")
            response = jsonify(error="Your changes could not be saved. Please try again.")
            response.status_code = 503
    response.headers["Cache-Control"] = "no-store" if not request.path.startswith("/static/") else "public, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response

@app.teardown_request
def close_storage(error=None):
    if hasattr(g, "store"): g.store.close()

def state():
    return g.state

def _broadcast(payload):
    # Clients poll committed database snapshots; no process-local event queues.
    pass

@app.route("/sign-in")
def sign_in():
    if session.get("site_authed") and auth.configured(): return redirect("/dashboard")
    return render_template("sign_in.html", configured=auth.configured(), local=not auth.hosted() and not os.environ.get("HERMES_USER"))

@app.route("/unlock", methods=["POST"])
def unlock():
    if not auth.configured(): return jsonify(error="Workspace setup is not complete."), 503
    data = request.get_json(silent=True) or request.form
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", data.get("pin", "")))
    # Shared durable rate limits work across serverless invocations.
    bucket = 'login:' + hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
    try:
        attempts = WorkspaceStore(app.config['DATABASE_PATH'], bucket, write=True)
        try:
            now = time.time()
            recent = [t for t in attempts.state.get('attempts', []) if now - t < 900]
            if len(recent) >= 5: return jsonify(error="Too many attempts. Please try again in 15 minutes."), 429
            valid = auth.verify(username, password)
            attempts.state['attempts'] = [] if valid else recent + [now]
            attempts.commit()
        finally: attempts.close()
    except Exception:
        return jsonify(error="Sign-in is temporarily unavailable. Please try again."), 503
    if valid:
        session.clear()
        session.permanent = True
        session["site_authed"] = True
        session["user_id"] = os.environ.get("HERMES_USER", "local") or "local"
        return jsonify(ok=True, next="/dashboard")
    return jsonify(error="Sign-in details were not recognized."), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")

# ── Encounter management ───────────────────────────────────────────────────────
EMPTY_INTAKE = {"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}
EMPTY_SCRIBE = {"subjective":"","physical_exam":"","assessment":"","plan":"","patient_instructions":"","follow_up":""}

STATUS_LABELS = {
    "waiting":   "Waiting",
    "intake":    "In Intake",
    "ready":     "Ready",
    "in_visit":  "In Visit",
    "complete":  "Complete",
}

def get_active_encounter() -> dict | None:
    if state()["active"] and state()["active"] in state()["encounters"]:
        return state()["encounters"][state()["active"]]
    return None

def safe_encounter(e: dict) -> dict:
    """Strip large note dicts for queue payload."""
    return {k: v for k, v in e.items() if k not in ("intake_note","scribe_note","transcript")}

def get_queue_payload() -> list:
    return [safe_encounter(state()["encounters"][eid]) for eid in state()["order"] if eid in state()["encounters"]]

def push_note(note_type: str, note: dict, fresh: bool = False):
    """Update globals + active encounter, then broadcast to all connected display clients.
    Set fresh=True when called from generate/scribe_generate. A fresh note bumps the
    """
    if note_type == "intake":
        state()["intake"] = note
    else:
        state()["scribe"] = note
    if fresh:
        state()["seq"] += 1
    active = get_active_encounter()
    if active:
        active[f"{note_type}_note"] = note
        active["updated_at"] = datetime.now().isoformat()
    _broadcast({"type": note_type, "data": note, "fresh": fresh, "seq": state()["seq"]})

def push_queue():
    _broadcast({"type": "queue", "data": get_queue_payload()})
    active = get_active_encounter()
    if active:
        _broadcast({"type": "patient", "data": safe_encounter(active)})

def push_generating(note_type: str):
    """Tell display clients a note is being generated so they show a loading skeleton."""
    _broadcast({"type": "generating", "note": note_type})

# ── Helpers ────────────────────────────────────────────────────────────────────
def safe_json_parse(text: str, fallback: dict | None = None) -> dict:
    try: return json.loads(text)
    except:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try: return json.loads(m.group(0))
            except: pass
    return fallback if fallback is not None else {}

# Fast transcription model — gpt-4o-mini-transcribe is markedly quicker (and more
# accurate) than whisper-1. Falls back to whisper-1 automatically if unavailable.
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")

def transcribe_audio(audio_bytes: bytes, prompt: str) -> str:
    try:
        r = client.with_options(timeout=25).audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=("recording.webm", audio_bytes, "audio/webm"),
            language="en", prompt=prompt,
        )
        return r.text.strip()
    except Exception:
        # Fallback keeps transcription working on any account/SDK version
        r = client.with_options(timeout=40).audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", audio_bytes, "audio/webm"),
            language="en", temperature=0, prompt=prompt,
        )
        return r.text.strip()

def chat(system_prompt: str, user_content: str, max_tokens: int = 900) -> str:
    r = client.with_options(timeout=30).chat.completions.create(
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
            "queue": get_queue_payload(),
            "transcript": state()["transcript"],
            "seq": state()["seq"],
        })
    return jsonify({
        "intake": state()["intake"],
        "scribe": state()["scribe"],
        "patient": None,
        "queue": get_queue_payload(),
        "transcript": state()["transcript"],
        "seq": state()["seq"],
    })

@app.route("/api/live_transcript", methods=["POST"])
def api_live_transcript():
    """Intake/scribe pages stream the running transcript here; we relay it to the
    provider display so the doctor can read the live conversation instantly."""
    text = (request.get_json(silent=True) or {}).get("text", "")
    if not isinstance(text, str) or len(text) > 100000:
        return jsonify({"error": "Transcript is too long or invalid."}), 400
    state()["transcript"] = text
    active = get_active_encounter()
    if active:
        active["transcript"] = state()["transcript"]
    _broadcast({"type": "transcript", "data": state()["transcript"]})
    return jsonify({"ok": True})

@app.route("/")
def home(): return render_template("landing.html")

@app.route("/dashboard")
def dashboard():
    encounters = list(reversed(get_queue_payload()))
    return render_template("dashboard.html", encounters=encounters, labels=STATUS_LABELS,
                           ai_connected=client is not None)

@app.route("/intake")
def intake(): return render_template("index.html")

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
    return jsonify(error="Live updates are available at /api/notes."), 410

# ── Patient / Encounter API ────────────────────────────────────────────────────
@app.route("/api/encounters", methods=["GET"])
def list_encounters():
    return jsonify(get_queue_payload())

@app.route("/api/encounters", methods=["POST"])
def add_encounter():
    data = request.json or {}
    mrn = (data.get("mrn") or "").strip()
    # If no MRN provided, auto-generate a visit label from room or sequence
    if not mrn:
        room_hint = (data.get("room") or "").strip()
        visit_num = len(state()["order"]) + 1
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
        "transcript":        "",
        "intake_note":  EMPTY_INTAKE.copy(),
        "scribe_note":  EMPTY_SCRIBE.copy(),
    }
    state()["encounters"][eid] = enc
    state()["order"].append(eid)

    # Auto-select if no active encounter
    state()["active"] = eid
    state()["transcript"] = ""

    push_queue()
    return jsonify(safe_encounter(enc))

@app.route("/api/encounters/<eid>", methods=["GET"])
def get_encounter(eid):
    if eid not in state()["encounters"]:
        return jsonify({"error": "Not found"}), 404
    return jsonify(state()["encounters"][eid])

@app.route("/api/encounters/<eid>/select", methods=["POST"])
def select_encounter(eid):
    if eid not in state()["encounters"]:
        return jsonify({"error": "Not found"}), 404
    state()["active"] = eid
    state()["transcript"] = state()["encounters"][eid].get("transcript", "")
    push_queue()
    active = state()["encounters"][eid]
    _broadcast({"type": "patient", "data": safe_encounter(active)})
    _broadcast({"type": "intake",  "data": active["intake_note"]})
    _broadcast({"type": "scribe",  "data": active["scribe_note"]})
    return jsonify({"ok": True})

@app.route("/api/encounters/<eid>/status", methods=["POST"])
def update_status(eid):
    if eid not in state()["encounters"]:
        return jsonify({"error": "Not found"}), 404
    new_status = (request.json or {}).get("status","")
    if new_status not in STATUS_LABELS:
        return jsonify({"error": "Invalid status"}), 400
    state()["encounters"][eid]["status"] = new_status
    state()["encounters"][eid]["updated_at"] = datetime.now().isoformat()
    push_queue()
    return jsonify({"ok": True})

@app.route("/api/encounters/<eid>", methods=["DELETE"])
def delete_encounter(eid):
    state()["encounters"].pop(eid, None)
    if eid in state()["order"]: state()["order"].remove(eid)
    if state()["active"] == eid:
        state()["active"] = state()["order"][0] if state()["order"] else None
    active = get_active_encounter()
    state()["transcript"] = active.get("transcript", "") if active else ""
    if not active:
        state()["intake"] = {}
        state()["scribe"] = {}
    push_queue()
    return jsonify({"ok": True})

@app.route("/api/encounters/<eid>/notes", methods=["PUT"])
def save_note(eid):
    enc = state()["encounters"].get(eid)
    if not enc: return jsonify(error="Encounter not found."), 404
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    allowed = EMPTY_INTAKE if kind == "intake" else EMPTY_SCRIBE if kind == "scribe" else None
    fields = data.get("fields", {})
    if allowed is None or not isinstance(fields, dict) or any(k not in allowed or not isinstance(v, str) or len(v) > 20000 for k,v in fields.items()):
        return jsonify(error="Invalid note fields."), 400
    enc[kind + "_note"].update(fields)
    if "transcript" in data:
        if not isinstance(data["transcript"], str): return jsonify(error="Invalid transcript."), 400
        enc["transcript"] = data["transcript"][:100000]
        if state()["active"] == eid: state()["transcript"] = enc["transcript"]
    enc["updated_at"] = datetime.now().isoformat()
    return jsonify(ok=True, saved_at=enc["updated_at"])

# ── Intake ─────────────────────────────────────────────────────────────────────
@app.route("/transcribe", methods=["POST"])
def transcribe():
    try:
        text = transcribe_audio(request.files["audio"].read(), INTAKE_WHISPER_PROMPT)
        # Auto-update status to "intake" when recording starts
        active = get_active_encounter()
        if active and active["status"] == "waiting":
            active["status"] = "intake"
            active["updated_at"] = datetime.now().isoformat()
            push_queue()
        return jsonify({"transcript": text})
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
        note = safe_json_parse(result)
        if not isinstance(note, dict) or any(not isinstance(note.get(k), str) for k in EMPTY_INTAKE):
            return jsonify({"error": "The generated note was incomplete. Please retry."}), 502
        push_note("intake", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate():
    try:
        push_generating("intake")
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
        if not isinstance(note, dict) or any(not isinstance(note.get(k), str) for k in EMPTY_INTAKE):
            return jsonify({"error": "The generated note was incomplete. Please retry."}), 502
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
    state()["intake"] = EMPTY_INTAKE.copy()
    state()["scribe"] = EMPTY_SCRIBE.copy()
    state()["transcript"] = ""
    push_note("intake", EMPTY_INTAKE.copy())
    push_note("scribe", EMPTY_SCRIBE.copy())
    active = get_active_encounter()
    if active: active["transcript"] = ""
    _broadcast({"type": "transcript", "data": ""})
    return jsonify({"ok": True})

# ── Scribe ─────────────────────────────────────────────────────────────────────
@app.route("/scribe_transcribe", methods=["POST"])
def scribe_transcribe():
    try:
        text = transcribe_audio(request.files["audio"].read(), SCRIBE_WHISPER_PROMPT)
        active = get_active_encounter()
        if active and active["status"] == "ready":
            active["status"] = "in_visit"
            active["updated_at"] = datetime.now().isoformat()
            push_queue()
        return jsonify({"transcript": text})
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
        note = safe_json_parse(result)
        if not isinstance(note, dict) or any(not isinstance(note.get(k), str) for k in EMPTY_SCRIBE):
            return jsonify({"error": "The generated note was incomplete. Please retry."}), 502
        push_note("scribe", note)
        return jsonify(note)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scribe_generate", methods=["POST"])
def scribe_generate():
    try:
        push_generating("scribe")
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
        if not isinstance(note, dict) or any(not isinstance(note.get(k), str) for k in EMPTY_SCRIBE):
            return jsonify({"error": "The generated note was incomplete. Please retry."}), 502
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
    state()["transcript"] = ""
    active = get_active_encounter()
    if active: active["transcript"] = ""
    push_note("scribe", EMPTY_SCRIBE.copy())
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
