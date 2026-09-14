"""
End-to-End-Latenztest gegen die echte, laufende Mantis-Backend-Instanz.
Schickt jeden Testsatz an /api/voice/segment (genau der Endpoint, den die
Desktop-App nutzt) und misst die Gesamtlaufzeit pro Anfrage vom Client aus —
das ist die reale Latenz, die Timo beim Sprechen erlebt.
"""
import glob
import time
import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.client_auth import dashboard_url, dashboard_headers
DATA_DIR = "data/stt_benchmark"


def main():
    base_url = dashboard_url()
    wavs = sorted(glob.glob(f"{DATA_DIR}/*.wav"))
    print(f"{len(wavs)} Testfälle gegen laufendes Backend ({base_url})\n")

    results = []
    with httpx.Client(timeout=60, headers=dashboard_headers()) as client:
        for wav_path in wavs:
            key = wav_path.rsplit("/", 1)[-1].replace(".wav", "")
            with open(wav_path, "rb") as f:
                audio_bytes = f.read()

            t0 = time.time()
            resp = client.post(
                f"{base_url}/api/voice/segment",
                files={"audio": (f"{key}.wav", audio_bytes, "audio/wav")},
            )
            total = time.time() - t0
            data = resp.json()
            results.append((key, total, data))
            print(f"{key:20s} {total:6.2f}s  addressed={data['addressed']!s:5s} text={data['text']!r}")
            if data.get("reply"):
                print(f"{'':20s}          reply={data['reply'][:80]!r}")

    print("\n=== Zusammenfassung ===")
    total_all = sum(r[1] for r in results)
    print(f"Gesamtdauer aller {len(results)} Anfragen: {total_all:.2f}s")
    print(f"Durchschnitt pro Anfrage: {total_all/len(results):.2f}s")
    addressed = [r for r in results if r[2]["addressed"]]
    if addressed:
        avg_addressed = sum(r[1] for r in addressed) / len(addressed)
        print(f"Durchschnitt bei adressierten Anfragen (voller Pfad STT+LLM+TTS): {avg_addressed:.2f}s")


if __name__ == "__main__":
    main()
