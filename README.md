# Study & Day

A study planner and assistant for keeping syllabuses, tracking completed topics, planning study days, managing daily tasks, and asking subject questions by chat or voice.

## Run locally

1. Install Python 3.10 or newer.
2. Install the Sarvam SDK:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and add your Sarvam API key after `SARVAM_API_KEY=`.
4. Start the app:

   ```powershell
   python app.py
   ```

Open `http://127.0.0.1:8767/` in your browser. Keep `.env` private. Account and study data are stored locally in `accounts.json` and `user_data/`.

