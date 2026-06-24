"""
receiver.py — Runs on the Raspberry Pi
Listens for TCP packets from the sender, validates them using the
embedded validation_rules that arrived with the packet itself,
and sends back a structured ACK.
"""

import socket
import json
import logging
import re

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9999
BUFFER_SIZE = 65536         # 64 KB — comfortably fits any sensor packet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Validation engine
# ─────────────────────────────────────────────
def _numeric(value: str) -> float:
    """Extract the first float/int from a string like '12.3 C'."""
    m = re.search(r"[-+]?\d*\.?\d+", value)
    if not m:
        raise ValueError(f"No numeric value found in {value!r}")
    return float(m.group())


def run_rule(rule: dict, payload: dict) -> dict:
    """
    Executes a single validation rule against the payload.
    Returns  { "rule_id", "passed": bool, "detail": str }
    """
    rid      = rule.get("rule_id", "UNKNOWN")
    field    = rule.get("field", "")
    check    = rule.get("check", "")
    expected = rule.get("expected", "")
    err_msg  = rule.get("error_msg", "Validation failed")

    field_value = payload.get(field)        # None if absent

    try:
        if check == "FIELD_EXISTS":
            passed = field in payload

        elif check == "VALUE_NOT_NULL":
            passed = field_value is not None and str(field_value).strip() != ""

        elif check == "EXACT_MATCH":
            passed = str(field_value) == str(expected)

        elif check == "CONTAINS_UNIT":
            passed = expected in str(field_value)

        elif check == "NUMERIC_VALUE":
            _numeric(str(field_value))
            passed = True

        elif check == "RANGE_CHECK":
            # expected format: "min to max"  e.g. "-50 to 150"
            m = re.match(r"([-\d.]+)\s+to\s+([-\d.]+)", str(expected))
            if not m:
                return {"rule_id": rid, "passed": False,
                        "detail": f"Bad RANGE_CHECK format in rule: {expected!r}"}
            lo, hi = float(m.group(1)), float(m.group(2))
            num    = _numeric(str(field_value))
            passed = lo <= num <= hi

        elif check == "REGEX_MATCH":
            passed = bool(re.search(expected, str(field_value)))

        elif check == "FORMAT_CHECK":
            # expected = comma-separated list of required keys
            required_keys = [k.strip() for k in expected.split(",")]
            passed = all(k in payload for k in required_keys)

        else:
            return {"rule_id": rid, "passed": False,
                    "detail": f"Unknown check type: {check!r}"}

        return {
            "rule_id": rid,
            "passed":  passed,
            "detail":  "OK" if passed else err_msg
        }

    except Exception as exc:
        return {"rule_id": rid, "passed": False, "detail": str(exc)}


def validate(packet: dict) -> dict:
    """
    Runs all rules in packet["validation_rules"] against packet["payload"].
    Returns a summary dict.
    """
    payload = packet.get("payload", {})
    rules   = packet.get("validation_rules", [])

    results      = [run_rule(r, payload) for r in rules]
    passed_count = sum(1 for r in results if r["passed"])
    all_passed   = passed_count == len(results)

    return {
        "all_passed":   all_passed,
        "passed":       passed_count,
        "total":        len(results),
        "rule_results": results
    }


# ─────────────────────────────────────────────
#  TCP server
# ─────────────────────────────────────────────
def handle_client(conn: socket.socket, addr):
    log.info(f"🔌 Connection from {addr}")

    try:
        # Receive until sender shuts down its write side
        chunks = []
        while True:
            chunk = conn.recv(BUFFER_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8")

        log.info(f"📥 Received {len(raw)} bytes from {addr}")

        # ── Parse ──────────────────────────────────
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError as e:
            ack = json.dumps({"status": "ERROR", "reason": f"Invalid JSON: {e}"})
            conn.sendall(ack.encode())
            return

        # ── Validate ───────────────────────────────
        validation  = validate(packet)
        sensor_type = packet.get("payload", {}).get("SENSOR_TYPE", "UNKNOWN")
        data        = packet.get("payload", {}).get("DATA", "N/A")

        # ── Log results ────────────────────────────
        status_icon = "✅" if validation["all_passed"] else "❌"
        log.info(
            f"{status_icon} Sensor: {sensor_type} | Data: {data} | "
            f"Rules: {validation['passed']}/{validation['total']} passed"
        )
        for r in validation["rule_results"]:
            icon = "  ✔" if r["passed"] else "  ✘"
            log.info(f"{icon} [{r['rule_id']}] {r['detail']}")

        # ── ACK back to sender ─────────────────────
        ack = json.dumps({
            "status":     "VALID" if validation["all_passed"] else "INVALID",
            "sensor":     sensor_type,
            "data":       data,
            "validation": validation
        })
        conn.sendall(ack.encode("utf-8"))

    except Exception as e:
        log.exception(f"💥 Error handling {addr}: {e}")
        try:
            conn.sendall(json.dumps({"status": "ERROR", "reason": str(e)}).encode())
        except Exception:
            pass
    finally:
        conn.close()
        log.info(f"🔒 Connection closed: {addr}")


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((LISTEN_HOST, LISTEN_PORT))
        server.listen(5)
        log.info(f"🟢 Receiver listening on {LISTEN_HOST}:{LISTEN_PORT}")

        while True:
            conn, addr = server.accept()
            handle_client(conn, addr)       # single-threaded; fine for sensor workloads


if __name__ == "__main__":
    main()
