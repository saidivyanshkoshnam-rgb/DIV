# DIV

A study planner and assistant for keeping syllabuses, tracking completed topics, planning study days, managing daily tasks, and asking subject questions by chat or voice.

## Run locally

1. Install Python 3.10 or newer.
2. Install the Sarvam SDK: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and add your Sarvam API key after `SARVAM_API_KEY=`.
4. Run `python app.py` and open `http://127.0.0.1:8765/`.

## Deploy a fresh hosted app

The Vercel setup serves the existing Python app. New accounts and study progress are saved in Supabase. Existing local account and study files are not copied.

1. Create a Supabase project and run [`supabase.sql`](supabase.sql) in its SQL Editor.
2. Import the GitHub repository into Vercel. Set the project root to the folder containing `app.py` and `vercel.json`.
3. Add these Vercel environment variables:
   - `SARVAM_API_KEY` — your rotated Sarvam key.
   - `SUPABASE_URL` — the Project URL from Supabase.
   - `SUPABASE_SERVICE_ROLE_KEY` — the server-side service role key from Supabase. Never put it in browser code or GitHub.
   - `SESSION_SECRET` — a private random value used to sign login sessions.
   - `PUBLIC_HTTPS` — `1`.
4. Deploy and open the Vercel URL.

The Supabase service role key stays on the server and is the only key allowed to access the private account and study tables. Keep `.env`, account files, and study data out of GitHub.
