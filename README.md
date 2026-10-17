# Hermes

Clinical intake, visit scribing, and a provider display by Pantheon Inventions.
Formerly ChartPilot. White, warm paper, charcoal, and Apollo solar gold.

## Local preview

Install requirements.txt with Python 3.12, copy .env.example to .env, and run
python app.py. On this computer, Open Hermes.cmd starts port 5056.
Local sign-in uses PIN 24601 unless SITE_PIN is changed. Encounters and edited
notes persist in .instance/hermes.sqlite. The session key is persisted locally.

Routes: / public introduction, /sign-in, /dashboard, /intake, /scribe, /display.
OPENAI_API_KEY enables transcription and generated notes. Manual notes work
without it. AI-generated content requires clinician review.

## Vercel: pantheon-hermes

Import this repository as a separate Flask project named pantheon-hermes.
vercel.json sets the request duration; .vercelignore excludes local data/runtime.
Configure these private environment variables before enabling hosted sign-in:

- SECRET_KEY: a unique cryptographically random secret.
- DATABASE_URL: a PostgreSQL connection string, including provider SSL settings.
- HERMES_USER: the workspace sign-in name.
- HERMES_PASSWORD_HASH: a Werkzeug generate_password_hash password hash.
- OPENAI_API_KEY: required for transcription and generation.

The homepage works without credentials. Hosted private routes fail closed when
required configuration is missing; the local prototype PIN never unlocks Vercel.
PostgreSQL stores a transactionally updated workspace snapshot per configured
account. The current sign-in supports one configured account, not a self-service
multi-clinic identity system. Provider display polls every three seconds.
Notes remain until their encounter is deleted. Provision database backups with
the hosting provider. Use synthetic encounters until clinical deployment,
access controls, retention and vendor agreements have been reviewed.

Apollo: https://pantheon-apollo.vercel.app

## Verification

python -m unittest -v test_app test_platform
node --test test_ui.cjs
node --check static/workspace.js

Tests use isolated synthetic databases and mocked AI responses, never paid AI calls.
