import os
from pathlib import Path

from sarvamai import SarvamAI

# Load the local .env value into the environment without adding a dependency.
for line in (Path(__file__).parent / ".env").read_text(encoding="utf-8-sig").splitlines():
    name, separator, value = line.partition("=")
    if separator and name.strip() == "SARVAM_API_KEY" and value.strip():
        os.environ[name.strip()] = value.strip()

if not os.environ.get("SARVAM_API_KEY"):
    raise SystemExit("Paste your API key into .env before running this script.")

client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
response = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[{"role": "user", "content": "Reply with a short hello."}],
)
print(response.choices[0].message.content)
