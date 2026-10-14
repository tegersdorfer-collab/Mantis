#!/usr/bin/env python3
"""Blindtest männlicher Google-Chirp3-HD-Stimmen (de-DE).

Aufruf: python3 scripts/tts_blindtest_google.py <ausgabeordner>

Liest GOOGLE_TTS_API_KEY aus der Umgebung bzw. ~/Mantis/.env, prüft per
GET /v1/voices, welche Kandidaten existieren, erzeugt je Satz eine MP3 und
schreibt die Latenz je Anfrage nach latenz.json. Der Key steht nur im Header.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

VOICES_URL = "https://texttospeech.googleapis.com/v1/voices"
SYNTH_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
LANGUAGE = "de-DE"
CANDIDATES = ("Charon", "Fenrir", "Orus", "Puck", "Achird")
SENTENCES = (
    "Es ist kurz nach Mitternacht, genau null Uhr siebenundvierzig.",
    "Ich hab den Branch gemerged und den Server neu gestartet, das Deployment läuft.",
    "Okay, Moment, ich schau kurz nach.",
    "Du hast heute keine Termine, aber das Meeting mit dem Team ist am Freitag um zehn.",
)
TIMEOUT_S = 15.0


def load_api_key() -> str:
    key = os.environ.get("GOOGLE_TTS_API_KEY", "").strip()
    if key:
        return key
    env_path = Path.home() / "Mantis" / ".env"
    try:
        from dotenv import dotenv_values

        return str((dotenv_values(env_path).get("GOOGLE_TTS_API_KEY") or "")).strip()
    except Exception:
        return ""


async def existing_male_voices(client: httpx.AsyncClient, headers: dict) -> list[str]:
    response = await client.get(VOICES_URL, params={"languageCode": LANGUAGE}, headers=headers)
    response.raise_for_status()
    available = {
        v["name"]
        for v in response.json().get("voices", [])
        if v.get("ssmlGender") == "MALE"
    }
    return [f"{LANGUAGE}-Chirp3-HD-{n}" for n in CANDIDATES if f"{LANGUAGE}-Chirp3-HD-{n}" in available]


async def run_blindtest(
    out_dir: Path, api_key: str, client: httpx.AsyncClient | None = None
) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json; charset=utf-8"}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        voices = await existing_male_voices(client, headers)
        results: dict = {"voices": {}, "missing": [
            f"{LANGUAGE}-Chirp3-HD-{n}" for n in CANDIDATES
            if f"{LANGUAGE}-Chirp3-HD-{n}" not in voices
        ]}
        for voice in voices:
            rows = []
            for i, sentence in enumerate(SENTENCES, 1):
                payload = {
                    "input": {"text": sentence},
                    "voice": {"languageCode": LANGUAGE, "name": voice},
                    "audioConfig": {"audioEncoding": "MP3", "sampleRateHertz": 24000},
                }
                start = time.perf_counter()
                response = await client.post(SYNTH_URL, headers=headers, json=payload)
                latency = time.perf_counter() - start
                response.raise_for_status()
                audio = base64.b64decode(response.json()["audioContent"])
                path = out_dir / f"{voice}_satz{i}.mp3"
                path.write_bytes(audio)
                rows.append({
                    "satz": i,
                    "datei": path.name,
                    "latenz_s": round(latency, 3),
                    "zeichen": len(sentence),
                    "bytes": len(audio),
                })
            results["voices"][voice] = rows
    finally:
        if own_client:
            await client.aclose()
    (out_dir / "latency.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    (out_dir / "latenz.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path, help="Ausgabeordner für MP3s und latenz.json")
    args = parser.parse_args()
    key = load_api_key()
    if not key:
        print("GOOGLE_TTS_API_KEY fehlt (Umgebung oder ~/Mantis/.env).", file=sys.stderr)
        return 1
    try:
        results = asyncio.run(run_blindtest(args.out_dir, key))
    except httpx.HTTPStatusError as exc:
        print(f"Google TTS HTTP {exc.response.status_code}", file=sys.stderr)
        return 1
    for voice, rows in results["voices"].items():
        avg = sum(r["latenz_s"] for r in rows) / len(rows)
        print(f"{voice}: Ø {avg:.2f}s")
    if results["missing"]:
        print("Nicht vorhanden:", ", ".join(results["missing"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
