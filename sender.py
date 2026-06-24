from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import socket
import json
import logging

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

OLLAMA_URL     = "http://localhost:11434/api/generate"
MODEL          = "packet-gen1"       # your custom Modelfile model
TARGET_HOST    = "100.117.250.88"     # Tailscale IP of Raspberry Pi
TARGET_PORT    = 9999
SOCKET_TIMEOUT = 10                 # seconds


# ─────────────────────────────────────────────
#  Step 1 — Ask the LLM to generate the packet
# ─────────────────────────────────────────────
def generate_packet(prompt: str) -> dict:
    """
    Calls the local Ollama model and returns a parsed dict with:
      {
        "payload":          { "SENSOR_TYPE": ..., "DATA": ... },
        "validation_rules": [ { rule_id, field, check, expected, error_msg }, ... ]
      }
    Raises ValueError if the model returns malformed JSON.
    """
    log.info(f"🧠 Sending prompt to model: {prompt!r}")

    resp = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "prompt": prompt, "stream": False},
        timeout=60
    )
    resp.raise_for_status()

    raw = resp.json().get("response", "").strip()
    log.info(f"📝 Raw model output:\n{raw}")

    # ── Strip accidental markdown fences ──────
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    try:
        packet = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model returned invalid JSON: {e}\nRaw output: {raw}")

    # ── Basic schema check ────────────────────
    if "payload" not in packet or "validation_rules" not in packet:
        raise ValueError(
            f"Model JSON missing 'payload' or 'validation_rules' keys.\n"
            f"Got keys: {list(packet.keys())}"
        )

    return packet


# ─────────────────────────────────────────────
#  Step 2 — Send over TCP (Tailscale)
# ─────────────────────────────────────────────
def send_tcp(packet: dict) -> str:
    """
    Serialises the full packet dict to JSON, sends it over a raw TCP
    socket to the Raspberry Pi, and returns the Pi's ACK string.
    """
    wire_data = json.dumps(packet)      # single serialisation — no double encoding

    log.info(f"📡 Connecting to {TARGET_HOST}:{TARGET_PORT}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(SOCKET_TIMEOUT)
        s.connect((TARGET_HOST, TARGET_PORT))
        log.info("✅ Connected")

        s.sendall(wire_data.encode("utf-8"))
        log.info(f"📦 Sent {len(wire_data)} bytes")

        # Signal end-of-message so the receiver knows we're done
        s.shutdown(socket.SHUT_WR)

        try:
            ack = s.recv(4096).decode("utf-8")
            log.info(f"📬 ACK from receiver: {ack}")
            return ack
        except socket.timeout:
            log.warning("⏱️  No ACK received within timeout — packet may still have arrived")
            return "NO_ACK_TIMEOUT"


# ─────────────────────────────────────────────
#  Flask routes
# ─────────────────────────────────────────────
@app.route("/generate", methods=["POST"])
def generate():
    body = request.get_json(silent=True) or {}
    user_prompt = body.get("prompt", "").strip()

    if not user_prompt:
        return jsonify({"error": "Missing 'prompt' in request body"}), 400

    try:
        packet          = generate_packet(user_prompt)
        server_response = send_tcp(packet)

        # Parse ACK if it's JSON (it will be from our receiver)
        try:
            server_response = json.loads(server_response)
        except (json.JSONDecodeError, TypeError):
            pass  # leave as plain string if not JSON

        return jsonify({
            "status":           "ok",
            "prompt":           user_prompt,
            "payload":          packet["payload"],
            "validation_rules": packet["validation_rules"],
            "server_response":  server_response
        })

    except ValueError as e:
        log.error(f"❌ Packet generation error: {e}")
        return jsonify({"error": str(e)}), 422

    except (ConnectionRefusedError, socket.timeout) as e:
        log.error(f"❌ TCP error: {e}")
        return jsonify({"error": f"Could not reach receiver: {e}"}), 503

    except Exception as e:
        log.exception("❌ Unexpected error")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "model":  MODEL,
        "target": f"{TARGET_HOST}:{TARGET_PORT}"
    })


# ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
