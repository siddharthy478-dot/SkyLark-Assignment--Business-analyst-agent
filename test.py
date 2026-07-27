
import os, requests
from dotenv import load_dotenv
load_dotenv()
key = os.environ.get('GEMINI_API_KEY')
resp = requests.get(
    'https://generativelanguage.googleapis.com/v1beta/models',
    headers={'x-goog-api-key': key},
)
import json
data = resp.json()
for m in data.get('models', []):
    if 'generateContent' in m.get('supportedGenerationMethods', []):
        print(m['name'])
