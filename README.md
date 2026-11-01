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

Open `http://127.0.0.1:8765/` in your browser. Keep `.env` private.

## Deploy publicly with Render

The repository includes `render.yaml` for a Render web service. The service uses a persistent disk for account and study data, listens on Render's public port, and reads the Sarvam key from the service's secret environment variables.

1. Push the project files, including `render.yaml`, to the root of your GitHub repository.
2. In Render, choose **New → Blueprint**, connect the GitHub repository, and select `render.yaml`.
3. Enter `SARVAM_API_KEY` in Render's secret prompt, then create the Blueprint.
4. When deployment finishes, open the `onrender.com` URL Render provides. Users can create an account with a username and password.

The Blueprint attaches a persistent disk, which Render only supports on a paid web service. Keep one running instance because the file-based account and study store is designed for a single instance. The app does not include password recovery yet; users should keep their passwords safe.

Never commit `.env`, account files, or user study data. The Render API key belongs in Render's environment settings, not in GitHub.
