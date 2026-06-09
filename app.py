from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from openai import OpenAI
from dotenv import load_dotenv
import json, re, threading, queue, uuid
from datetime import datetime

load_dotenv()
app = Flask(__name__)
client = OpenAI()

# ── ENT Vocabulary ─────────────────────────────────────────────────────────────
ENT_VOCAB = """
mometasone, azelastine, fluticasone, Flonase, Nasacort, Rhinocort, Nasonex, Dymista, Astepro,
ipratropium, Atrovent, Afrin, oxymetazoline, saline spray, NeilMed, Xhance, Zyrtec, cetirizine,
Claritin, loratadine, Allegra, fexofenadine, Xyzal, levocetirizine, Benadryl, diphenhydramine,
Singulair, montelukast, Sudafed, pseudoephedrine, Mucinex, guaifenesin, amoxicillin, Augmentin,
amoxicillin-clavulanate, cefdinir, Omnicef, azithromycin, Z-Pak, doxycycline, clindamycin,
Bactrim, Levaquin, levofloxacin, ciprofloxacin, Cipro, prednisone, Medrol, methylprednisolone,
dexamethasone, Decadron, Kenalog, triamcinolone, ofloxacin, Floxin, Ciprodex, Cortisporin,
Debrox, carbamide peroxide, Vosol, fluocinolone, DermOtic, omeprazole, Prilosec, pantoprazole,
Protonix, famotidine, Pepcid, Nexium, esomeprazole, lansoprazole, Prevacid, meclizine, Antivert,
ondansetron, Zofran, promethazine, Phenergan, scopolamine, diazepam, Valium, acetaminophen,
ibuprofen, naproxen, aspirin, otalgia, otorrhea, tinnitus, vertigo, rhinorrhea, postnasal drip,
dysphagia, odynophagia, dysphonia, globus, epistaxis, anosmia, hyposmia, tympanic membrane,
Eustachian tube, turbinates, maxillary sinus, frontal sinus, ethmoid sinus, sphenoid sinus,
vocal cords, nasopharynx, oropharynx, hypopharynx, audiogram, tympanogram, laryngoscopy,
CT sinus, myringotomy, tympanostomy, cerumen, septoplasty, turbinate reduction, tonsillectomy,
adenoidectomy, balloon sinuplasty
"""

EMPTY_NOTE = {
    "chief_complaint": "",
    "hpi": "",
    "ros": "",
    "medications": "",
    "allergies": "",
    "doctor_handoff": ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Server-side state
# Both MA and doctor views read from here. After every transcription cycle,
# the structured note is stored here and pushed via SSE to the doctor screen.
# ─────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "transcript": "",
    "note": dict(EMPTY_NOTE),   # ← structured fields the doctor view renders
    "status": "ready",          # ready | listening | processing | complete
    "encounter_id": str(uuid.uuid4()),
    "last_updated": None
}

# SSE subscriber queues — one per connected doctor view tab
_subs_lock = threading.Lock()
_subscribers: list[queue.Queue] = []


def _push():
    """Push current server state to every connected doctor-view subscriber."""
    with _lock:
        payload = json.dumps(_state)
    with _subs_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for d in dead:
            _subscribers.remove(d)


def _set(**kw):
    """Update state fields and push immediately."""
    with _lock:
        _state.update(kw)
        _state["last_updated"] = datetime.now().isoformat()
    _push()


def safe_json(text: str, fallback=None) -> dict:
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return fallback or dict(EMPTY_NOTE)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/doctor")
def doctor():
    return render_template("doctor.html")


@app.route("/stream")
def stream():
    """
    SSE endpoint. The doctor view connects here and receives every note update
    in real time. Each message is the full _state JSON so the client is always
    in sync, even if it connects mid-encounter.
    """
    def generate():
        q: queue.Queue = queue.Queue(maxsize=30)
        with _subs_lock:
            _subscribers.append(q)
        try:
            # Immediately send current state on connect
            with _lock:
                yield f"data: {json.dumps(_state)}\n\n"
            while True:
                try:
                    data = q.get(timeout=20)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield 'data: {"ping":true}\n\n'   # keep-alive heartbeat
        except GeneratorExit:
            pass
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/state")
def get_state():
    """Fallback polling endpoint (also used by doctor view on reconnect)."""
    with _lock:
        return jsonify(dict(_state))


@app.route("/transcribe", methods=["POST"])
def transcribe():
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"transcript": ""})

    try:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=("recording.webm", audio.stream, "audio/webm")
        )
        raw = result.text.strip()
        if not raw:
            return jsonify({"transcript": ""})

        # Correct ENT-specific terminology that Whisper commonly mishears
        correction = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You fix speech-to-text errors from an ENT clinic intake conversation.\n\n"
                        f"ENT vocabulary reference:\n{ENT_VOCAB}\n\n"
                        "Examples of common errors:\n"
                        "- 'flow nays' → 'Flonase'\n"
                        "- 'sir tech' → 'Zyrtec'\n"
                        "- 'my metazone' → 'mometasone'\n"
                        "- 'augmenton' → 'Augmentin'\n"
                        "- 'Dimetapp' → 'Dymista'\n\n"
                        "Rules: Fix obvious errors only. Do NOT add information. "
                        "Do NOT summarize. Return ONLY the corrected transcript."
                    )
                },
                {"role": "user", "content": raw}
            ]
        )
        corrected = correction.choices[0].message.content.strip()
        return jsonify({"transcript": corrected})

    except Exception as e:
        app.logger.error(f"/transcribe error: {e}")
        return jsonify({"transcript": "", "error": str(e)}), 500


@app.route("/live_update", methods=["POST"])
def live_update():
    """
    Called after each 10-second audio cycle in live mode.
    Updates the structured note from the running transcript,
    saves it server-side, and pushes it to the doctor view via SSE.
    """
    body = request.json or {}
    transcript = body.get("transcript", "").strip()

    if not transcript:
        return jsonify(EMPTY_NOTE)

    # Use the server-side note as context so we don't lose prior info
    with _lock:
        current_note = dict(_state["note"])

    try:
        _set(status="processing")

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.15,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You update a real-time ENT intake note from a live conversation transcript.\n\n"
                        "RULES:\n"
                        "- Only document what was explicitly stated\n"
                        "- Do NOT diagnose or create treatment plans\n"
                        "- Preserve useful existing information — only update a field when new info was mentioned\n"
                        "- If a field has no information yet, write exactly: Not discussed.\n"
                        "- Medications: include ALL (Rx, OTC, nasal sprays, antihistamines, antibiotics, steroids, ear drops)\n"
                        "- If patient denies meds: 'Patient denies current medications.'\n"
                        "- Return ONLY valid JSON — no commentary, no markdown fences\n\n"
                        "Return exactly this structure:\n"
                        '{"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}'
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Current note (preserve this, only update with new info):\n"
                        f"{json.dumps(current_note)}\n\n"
                        f"Running transcript:\n{transcript}"
                    )
                }
            ]
        )

        note = safe_json(resp.choices[0].message.content, current_note)

        # ← THE KEY FIX: save note server-side and push to doctor view
        _set(transcript=transcript, note=note, status="listening")

        return jsonify(note)

    except Exception as e:
        app.logger.error(f"/live_update error: {e}")
        _set(status="listening")
        return jsonify(current_note), 500


@app.route("/generate", methods=["POST"])
def generate():
    """
    Final note generation at end of encounter.
    Uses gpt-4o for higher quality. Pushes 'complete' status to doctor view.
    """
    body = request.json or {}
    transcript = body.get("transcript", "").strip()

    if not transcript:
        return jsonify(EMPTY_NOTE)

    try:
        _set(status="processing")

        resp = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an ENT intake assistant generating a final structured note for physician review.\n\n"
                        "RULES:\n"
                        "- Only document what was explicitly stated in the transcript\n"
                        "- If not discussed, write exactly: Not discussed.\n"
                        "- Do NOT diagnose or suggest treatment\n"
                        "- Be concise and clinically precise\n"
                        "- Medications: include ALL (Rx, OTC, nasal sprays, antihistamines, antibiotics, steroids, ear drops)\n"
                        "- If denied: 'Patient denies current medications.'\n"
                        "- Return ONLY valid JSON — no commentary, no markdown fences\n\n"
                        "Return exactly this structure:\n"
                        '{"chief_complaint":"","hpi":"","ros":"","medications":"","allergies":"","doctor_handoff":""}'
                    )
                },
                {
                    "role": "user",
                    "content": f"Full encounter transcript:\n{transcript}"
                }
            ]
        )

        note = safe_json(resp.choices[0].message.content)

        # Push final note + 'complete' status to doctor view
        _set(transcript=transcript, note=note, status="complete")

        return jsonify(note)

    except Exception as e:
        app.logger.error(f"/generate error: {e}")
        _set(status="ready")
        return jsonify(EMPTY_NOTE), 500


@app.route("/ai_edit", methods=["POST"])
def ai_edit():
    """
    Doctor-initiated AI editing of a single note field.
    Supports: improve | rewrite | expand | custom
    Returns the rewritten text and updates server state so the MA view stays in sync.
    """
    body    = request.json or {}
    field   = body.get("field", "")          # e.g. "chief_complaint"
    content = body.get("content", "").strip()
    action  = body.get("action", "improve")  # improve | rewrite | expand | custom
    custom  = body.get("custom_instruction", "").strip()

    if not content or content in ("Not discussed.", "Awaiting encounter…"):
        return jsonify({"content": content, "error": "Nothing to edit"}), 400

    # Context hint so the model knows what each field represents
    field_context = {
        "chief_complaint": "Chief Complaint (CC) — primary reason for the visit, one or two sentences",
        "hpi":             "History of Present Illness (HPI) — onset, duration, severity, quality, location, radiation, modifying factors, associated symptoms",
        "ros":             "Review of Systems (ROS) — relevant positive and negative findings by system",
        "medications":     "Medications — current Rx and OTC medications with doses and frequency where known",
        "allergies":       "Allergies — medication and environmental allergies with reactions noted",
        "doctor_handoff":  "Doctor Handoff — high-yield clinical summary for the physician before entering the room"
    }

    action_instructions = {
        "improve": (
            "Improve the clinical language, grammar, and organization of this note field. "
            "Keep all the same information but make it more concise, precise, and professionally written. "
            "Do not add new patient facts."
        ),
        "rewrite": (
            "Rewrite this note field from scratch in clear, structured clinical language. "
            "Retain all stated facts but reorganize and reformat for maximum clinical clarity "
            "using standard ENT documentation style."
        ),
        "expand": (
            "Expand this note field with more clinical detail and standard ENT documentation phrasing. "
            "Add contextual clinical language and typical follow-up details that would normally be "
            "documented (e.g. laterality, duration, severity scale, aggravating/relieving factors, "
            "associated symptoms). Do NOT invent specific patient facts — only add clinically "
            "appropriate framing and structure."
        ),
        "custom": (
            f"Follow this instruction exactly: {custom}\n"
            "Apply it to improve the note field while keeping it clinically appropriate."
        )
    }

    instruction = action_instructions.get(action, action_instructions["improve"])

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.25,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an ENT clinical documentation specialist editing a structured intake note.\n\n"
                        f"Field type: {field_context.get(field, field)}\n\n"
                        f"Task: {instruction}\n\n"
                        "Rules:\n"
                        "- Write in clinical note style, NOT conversational prose\n"
                        "- Do NOT diagnose or suggest treatment plans\n"
                        "- Do NOT add headers or labels — return only the field text itself\n"
                        "- Return ONLY the edited text, nothing else"
                    )
                },
                {"role": "user", "content": content}
            ]
        )
        improved = resp.choices[0].message.content.strip()

        # Update server state so MA view preview also reflects the edit
        with _lock:
            if field in _state["note"]:
                _state["note"][field]  = improved
                _state["last_updated"] = datetime.now().isoformat()
        _push()

        return jsonify({"content": improved})

    except Exception as e:
        app.logger.error(f"/ai_edit error: {e}")
        return jsonify({"content": content, "error": str(e)}), 500


@app.route("/clear", methods=["POST"])
def clear():
    with _lock:
        _state["transcript"] = ""
        _state["note"] = dict(EMPTY_NOTE)
        _state["status"] = "ready"
        _state["encounter_id"] = str(uuid.uuid4())
        _state["last_updated"] = datetime.now().isoformat()
    _push()
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
