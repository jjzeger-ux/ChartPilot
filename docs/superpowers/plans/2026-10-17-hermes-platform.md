# Hermes Platform Implementation Plan

**Goal:** Public Hermes introduction, authenticated dashboard, durable encounters,
and a separate Pantheon Vercel deployment.

**Architecture:** Keep Flask and the existing recording pages. Add a shared shell,
landing page and dashboard. Replace process globals with request-scoped state in
transactional storage: SQLite locally, PostgreSQL when hosted. Use configured
single-owner credentials for this prototype; no claim of multi-user clinical readiness.

**Constraints:** Preserve white/gold identity, Apollo link, existing AI prompts and
note fields. Hosted workspaces fail closed without credentials and shared storage.
No clinical data, local databases, or credentials in Git or deployment bundles.
No purchase or provider agreement without user approval.

## Tasks
- [ ] 1. Test public/private routing, failed login, persistence, account separation,
  note save/reopen, missing hosted configuration, and rollback on failed mutations.
- [ ] 2. Implement storage.py and auth.py; adapt app.py to request-scoped state.
  Replace indefinite process-local SSE with bounded database-backed polling.
- [ ] 3. Build landing, sign-in, dashboard and shared shell. Add navigation and
  autosave/reopen to intake and scribe, with visible save failures.
- [ ] 4. Verify Python and JavaScript tests, desktop/mobile browser flows, and
  independent code review. Fix concrete issues before publication.
- [ ] 5. Package only Hermes, configure Vercel project pantheon-hermes, deploy
  public page; configure private workspace only when account/database values are
  available. Verify live domain and report any remaining configuration explicitly.

## Review focus
- Missing database or secret must never expose a hosted workspace.
- A failed AI call must not replace saved notes.
- Switching encounters must not carry the previous patient's text into a new note.
- Reloading and server restart must preserve saved text.
- Public pages must not disclose private encounter metadata.

## Execution notes
The user approved the public-page/sign-in/dashboard direction and asked to proceed.
Continue within the existing isolated Hermes checkout, preserving earlier remodel
changes; use a feature branch. Vercel is explicitly requested, so Sites hosting is
not used. External database provisioning may require user action or terms acceptance.
