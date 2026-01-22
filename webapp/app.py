from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, Response
import paho.mqtt.publish as publish
import paho.mqtt.client as mqtt
import threading
import json
import os
import time
import queue
import html
import re


app = Flask(__name__)
app.secret_key = "something-secret"

# Default MQTT
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt_backend")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

DATA_DIR = "data"
APPROVED_FILE = os.path.join(DATA_DIR, "microbits.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_microbits.json")

os.makedirs(DATA_DIR, exist_ok=True)

# Simple ID validation: only letters, numbers, dash, underscore
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


#########################################################
# SSE (Server Sent Events) — supports multiple clients
#########################################################
# SSE — robust, gevent-friendly implementation
from gevent.queue import Queue, Empty
import uuid
import traceback

clients = {}  # client_id -> Queue()

def send_event(event_type, data):
    """
    Broadcast event_type + data to all connected SSE clients.
    - data will be JSON-serialized (or coerced to string) to avoid invalid payloads.
    - dead clients will be pruned.
    """
    # sanitize/serialize data
    if data is None:
        payload = ""
    elif isinstance(data, str):
        payload = data
    else:
        try:
            payload = json.dumps(data)
        except Exception:
            # fallback: safe string repr
            payload = json.dumps({"_error_serializing": True, "value": str(data)})

    dead = []
    for cid, q in list(clients.items()):
        try:
            q.put_nowait((event_type, payload))
        except Exception as e:
            print(f"[SSE] failed to enqueue for {cid}: {e}")
            dead.append(cid)

    for cid in dead:
        clients.pop(cid, None)

    print(f"[SSE] broadcast: {event_type} -> {payload}")

@app.route("/events")
def sse_stream():
    """
    Create a per-connection gevent Queue and stream events from it.
    Returns Response with headers to disable buffering.
    """

    def gen(client_id, q: Queue):
        try:
            print(f"[SSE] gen started for {client_id}")
            while True:
                try:
                    event_type, payload = q.get(timeout=15)  # wait for real event
                    # protect yield so exceptions here don't kill the whole worker
                    try:
                        yield f"event: {event_type}\ndata: {payload}\n\n"
                    except Exception as ye:
                        # log yield error then continue (don't crash worker)
                        print(f"[SSE] yield error for {client_id}: {ye}")
                        traceback.print_exc()
                        # try to notify the client (safe text)
                        try:
                            yield ": error\n\n"
                        except Exception:
                            pass
                except Empty:
                    # Keep-alive comment so proxies don't time out
                    try:
                        yield ": keep-alive\n\n"
                    except Exception:
                        # if the yield fails, break and clean up
                        print(f"[SSE] keep-alive yield failed for {client_id}")
                        break
                except Exception as e:
                    # log and keep loop running (avoid bringing down worker)
                    print(f"[SSE] queue.get error for {client_id}: {e}")
                    traceback.print_exc()
                    # brief sleep to avoid tight loop on error
                    import gevent
                    gevent.sleep(0.1)
        except GeneratorExit:
            # normal client disconnect
            print(f"[SSE] client {client_id} disconnected (GeneratorExit)")
        except Exception as e:
            print(f"[SSE] gen unexpected error for {client_id}: {e}")
            traceback.print_exc()
        finally:
            # ensure client entry removed
            clients.pop(client_id, None)
            print(f"[SSE] cleanup done for {client_id}")

    client_id = str(uuid.uuid4())
    q = Queue()
    clients[client_id] = q
    print(f"[SSE] client connected: {client_id} (total={len(clients)})")

    # Important headers: disable buffering in nginx/gunicorn proxies
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    return Response(gen(client_id, q), mimetype="text/event-stream", headers=headers)


#########################################################
# JSON persistence helpers
#########################################################

def load_list(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        data = json.load(f)
    # sanitize and ensure defaults
    for mb in data:
        # ensure keys exist and sanitize strings
        mb["id"] = str(mb.get("id", ""))
        if not ID_RE.match(mb["id"]):
            # if ID invalid, drop or sanitize — here we replace invalid chars with underscore
            mb["id"] = re.sub(r"[^A-Za-z0-9_-]", "_", mb["id"])

        # sanitize textual fields
        mb["name"] = html.escape(str(mb.get("name", "")))
        mb["location"] = html.escape(str(mb.get("location", "")))
        mb["message"] = html.escape(str(mb.get("message", "")))
        # ensure broadcast flag exists
        if "broadcast" not in mb:
            mb["broadcast"] = False
    return data

def save_list(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        

# Load microbit lists
MICROBITS = load_list(APPROVED_FILE)
PENDING = load_list(PENDING_FILE)

print("\nLoaded approved microbits:", MICROBITS)
print("Loaded pending microbits:", PENDING, "\n")


#########################################################
# MQTT broker update 
#########################################################

@app.route("/mqtt/update", methods=["POST"])
def update_mqtt():
    data = request.json
    host = data.get("host")
    port = data.get("port", 1883)

    if not host:
        return jsonify({"success": False, "msg": "Missing host"}), 400

    global MQTT_HOST, MQTT_PORT
    MQTT_HOST = host
    MQTT_PORT = port

    print(f"MQTT settings updated from browser → host={host}, port={port}")

    return jsonify({"success": True})


#########################################################
# MQTT listener to capture registration messages
#########################################################

############################################
# Robust MQTT Manager (replace your old listener)
############################################

class MQTTManager:
    def __init__(self, host, port, topics=None, keepalive=60):
        self.host = host
        self.port = port
        self.keepalive = keepalive

        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message_internal

        # reconnect backoff handled by paho
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        # topics is a list of (topic, qos)
        self.topics = topics or []

        # outgoing queue: (topic, payload, qos)
        self._out_q = queue.Queue()

        # event that signals connection state
        self.connected_event = threading.Event()

        # worker threads
        self._publisher_thread = None
        self._running = False

        # user-provided callback for messages (set externally)
        # signature: def user_on_message(client, userdata, msg)
        self.user_on_message = None

        # lock for updating connection (host/port)
        self._conn_lock = threading.Lock()

    # -------------------------
    # Public API
    # -------------------------
    def start(self):
        """Start mqtt networking and publisher worker."""
        with self._conn_lock:
            if self._running:
                return
            self._running = True

            # async connect so app doesn't block, and so paho can auto-reconnect
            self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
            self.client.loop_start()

            # start publisher worker
            self._publisher_thread = threading.Thread(target=self._publisher_worker, daemon=True)
            self._publisher_thread.start()

            print("[MQTTManager] started")

    def stop(self):
        """Stop threads and disconnect cleanly."""
        with self._conn_lock:
            self._running = False
            try:
                self.client.loop_stop(force=False)
                self.client.disconnect()
            except Exception as e:
                print("[MQTTManager] stop error:", e)
            # wake publisher thread
            try:
                self._out_q.put_nowait((None, None, None))
            except Exception:
                pass

    def publish(self, topic, payload, qos=0):
        """Thread-safe enqueue publish. Will be sent when connected."""
        # Convert payload to str/bytes here so queue contains serializable items
        if payload is None:
            payload_send = ""
        elif isinstance(payload, (dict, list)):
            payload_send = json.dumps(payload)
        else:
            payload_send = str(payload)

        # Put into queue; publisher worker handles connection state
        self._out_q.put((topic, payload_send, qos))

    def update_broker(self, host, port):
        """Change broker at runtime. Will reconnect to new broker."""
        with self._conn_lock:
            self.host = host
            self.port = port
            # Restart connection to new broker
            try:
                self.client.disconnect()
            except Exception:
                pass
            # reconnect async to new host/port
            self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
            # connected_event will be updated by on_connect/on_disconnect
            print(f"[MQTTManager] broker updated to {self.host}:{self.port}")

    # -------------------------
    # Internal: callbacks
    # -------------------------
    def _on_connect(self, client, userdata, flags, rc):
        print(f"[MQTTManager] connected (rc={rc})")
        # mark connected
        self.connected_event.set()
        # resubscribe to topics
        for t, qos in self.topics:
            try:
                client.subscribe(t, qos)
                print(f"[MQTTManager] subscribed: {t}")
            except Exception as e:
                print(f"[MQTTManager] subscribe failed: {t} -> {e}")

    def _on_disconnect(self, client, userdata, rc):
        print(f"[MQTTManager] disconnected (rc={rc})")
        # clear connected flag so publisher worker knows to hold messages
        self.connected_event.clear()
        # paho will try to reconnect due to connect_async + reconnect_delay_set

    def _on_message_internal(self, client, userdata, msg):
        """Internal wrapper that forwards to user callback safely."""
        try:
            if self.user_on_message:
                # Forward to user-defined handler (should be non-blocking)
                self.user_on_message(client, userdata, msg)
            else:
                # default debug print
                print("[MQTTManager] message:", msg.topic, msg.payload)
        except Exception as e:
            # Never let exceptions bubble up out of the mqtt thread
            print("[MQTTManager] user on_message error:", e)

    # -------------------------
    # Internal: publisher worker
    # -------------------------
    def _publisher_worker(self):
        """
        Worker that drains the outgoing queue and calls client.publish.
        If not connected, it will wait and retry (without dropping messages).
        """
        while self._running:
            try:
                item = self._out_q.get(timeout=1.0)
            except queue.Empty:
                continue

            # sentinel to shutdown
            if item == (None, None, None):
                break

            topic, payload, qos = item

            # Wait until connected (but don't block forever if shutting down)
            # Wait in small increments to respond to stop quickly.
            waited = 0.0
            while self._running and not self.connected_event.is_set():
                # Retry logic: try every 0.5s while disconnected
                time.sleep(0.5)
                waited += 0.5
                # continue waiting

            if not self._running:
                break

            # Try to publish (paho's publish is thread-safe)
            try:
                # This call is non-blocking; returns MQTTMessageInfo
                info = self.client.publish(topic, payload=payload, qos=qos)
                # For QoS>0 you may wait for publish to complete:
                # info.wait_for_publish(timeout=5)
            except Exception as e:
                # On failure, requeue the message and back off a little
                try:
                    self._out_q.put_nowait((topic, payload, qos))
                except Exception:
                    # if requeue fails, log and drop (very rare)
                    print("[MQTTManager] failed to requeue message, dropping:", topic, e)
                time.sleep(1)

        print("[MQTTManager] publisher worker exiting")

def on_message(client, userdata, msg):
    topic = msg.topic

    # Auto-registration topic: microbits/register/<id>
    if topic.startswith("microbits/register/"):
        dev_id_raw = topic.split("/")[-1]
        # sanitize id
        dev_id = re.sub(r"[^A-Za-z0-9_-]", "_", dev_id_raw)

        # Only add if completely new
        if dev_id not in [m["id"] for m in MICROBITS] and dev_id not in PENDING:
            PENDING.append(dev_id)
            save_list(PENDING_FILE, PENDING)
            send_event("refresh", "refresh")
            print(f"New pending microbit registered: {dev_id}")
        return

    # Config request: microbits/<id>/configrequest
    if topic.endswith("/configrequest"):
        parts = topic.split("/")
        if len(parts) >= 3:
            _, microbit_id_raw, _ = parts[:3]
            microbit_id = re.sub(r"[^A-Za-z0-9_-]", "_", microbit_id_raw)
        else:
            return

        mb = next((m for m in MICROBITS if m["id"] == microbit_id), None)
        if not mb:
            print(f"Config request for unknown microbit: {microbit_id}")
            return

        mqtt_manager.publish(f"microbits/{microbit_id}/config/radio",
                             str(mb["radio"]))

        mqtt_manager.publish(f"microbits/{microbit_id}/config/message",
                             str(mb["message"]))
        
        mqtt_manager.publish(f"microbits/{microbit_id}/config/broadcast",
                             str(mb["broadcast"])) 

        print(f"[MQTT] Config sent to {microbit_id}")

# -------------------------
# Integration helpers (adapt to your app)
# -------------------------

# Create manager (example)
mqtt_manager = MQTTManager(MQTT_HOST, MQTT_PORT, topics=[("microbits/register/#", 0), ("microbits/+/configrequest", 0)])
mqtt_manager.user_on_message = on_message  # set your handler
mqtt_manager.start()
#
# Then replace publish.single(...) with: mqtt_manager.publish(topic, payload)
#
# If you want to change broker at runtime (your /mqtt/update route): mqtt_manager.update_broker(new_host, new_port)




#########################################################
# Routes
#########################################################

@app.route("/")
def index():
    return render_template("index.html", microbits=MICROBITS, pending=PENDING)

@app.route("/control", methods=["POST"])
def control():
    mb_id = request.form["id"]
    action = request.form["action"]

    # sanitize id
    if not ID_RE.match(mb_id):
        return jsonify({"message": "Invalid microbit id"}), 400

    topic = f"microbits/{mb_id}/{action}"

    if action == "remove":
        global MICROBITS
        MICROBITS = [m for m in MICROBITS if m["id"] != mb_id]
        save_list(APPROVED_FILE, MICROBITS)
        send_event("refresh", "refresh")
        return jsonify({"message": f"Removed {mb_id}"})

    elif action == "ping":
        topic = f"microbits/{mb_id}/ping"
        mqtt_manager.publish(topic, str("ping"))
        flash(f"Ping sent to {mb_id}")
        mqtt_manager.publish(topic, f"{mb_id}:{action}")
        return jsonify({"message": f"{action.upper()} sent to {mb_id}"})
    

    elif action == "start":
        mb = next((m for m in MICROBITS if m["id"] == mb_id), None)
        if not mb:
            return jsonify({"message": "Microbit not found"}), 404
        mb["broadcast"] = True
        save_list(APPROVED_FILE, MICROBITS)
        send_event("refresh", "refresh")
        mqtt_manager.publish(topic, f"{mb_id}:{action}")
        return jsonify({"message": f"Send enabled for {mb_id}"})
    

    elif action == "stop":
        mb = next((m for m in MICROBITS if m["id"] == mb_id), None)
        if not mb:
            return jsonify({"message": "Microbit not found"}), 404
        mb["broadcast"] = False
        save_list(APPROVED_FILE, MICROBITS)
        send_event("refresh", "refresh")
        mqtt_manager.publish(topic, f"{mb_id}:{action}")
        return jsonify({"message": f"Send disabled for {mb_id}"})
    

    elif action == "update_config":
        # Find the microbit data
        mb = next((m for m in MICROBITS if m["id"] == mb_id), None)
        if not mb:
            return jsonify({"message": "Microbit not found"}), 404
        
        
        # Publish radio as a string
        topic_radio = f"microbits/{mb_id}/config/radio"
        mqtt_manager.publish(topic_radio, str(mb["radio"]))
        
        # Publish message as a string
        topic_message = f"microbits/{mb_id}/config/message"
        mqtt_manager.publish(topic_message, str(mb["message"]))

        # Publish message as a string
        topic_broadcast = f"microbits/{mb_id}/config/broadcast"
        mqtt_manager.publish(topic_broadcast, str(mb["broadcast"]))

        return jsonify({"message": f"Radio and message sent to {mb_id}!"})    
    
    mqtt_manager.publish(topic, f"{mb_id}:{action}")
    send_event("refresh", "refresh")  # notify clients if needed 

    #flash(f"Micro:bit {mb_id} has been {action}ed.", "info")
    return jsonify({"message": f"{action.upper()} sent to {mb_id}"})


#########################################################
# Add a microbit manually
#########################################################

@app.route("/add", methods=["POST"])
def add_microbit():
    new_id_raw = request.form["new_id"].strip()
    name_raw = request.form["name"].strip()
    location_raw = request.form["location"].strip()
    radio_raw = request.form["radio"].strip()
    message_raw = request.form["message"].strip()
    broadcast = False

    # Validate ID
    if not new_id_raw or not ID_RE.match(new_id_raw):
        return jsonify({"success": False, "message": "ID must be letters, numbers, - or _"}), 400

    # sanitize stored values
    new_id = new_id_raw
    name = html.escape(name_raw)
    location = html.escape(location_raw)
    message = html.escape(message_raw)

    # --- Radio validation ---
    try:
        radio = int(radio_raw)
        if not 0 <= radio <= 255:
            return jsonify({"success": False, "message": "Radio must be between 0 and 255."})
    except ValueError:
        return jsonify({"success": False, "message": "Radio must be a valid integer."})

    if not new_id:
        return jsonify({"success": False, "message": "ID cannot be empty."})

    if new_id in [m["id"] for m in MICROBITS]:
        return jsonify({"success": False, "message": "Microbit already approved."})

    if new_id in PENDING:
        PENDING.remove(new_id)
        save_list(PENDING_FILE, PENDING)

    MICROBITS.append({
        "id": new_id,
        "name": name or "Unnamed",
        "location": location or "Unknown",
        "radio": radio,
        "message": message or "Unknown",
        "broadcast": broadcast or False
    })
    save_list(APPROVED_FILE, MICROBITS)
    send_event("refresh", "refresh")

    return jsonify({"success": True, "message": f"Microbit {new_id} added."})

#########################################################
# Approve pending microbit
#########################################################

@app.route("/approve/<id>", methods=["POST"])
def approve(id):
    if id in PENDING:
        PENDING.remove(id)
        save_list(PENDING_FILE, PENDING)

        MICROBITS.append({
            "id": id,
            "name": "Unnamed",
            "location": "Unknown",
            "radio": "Unknown",
            "message": "Unknown",
            "broadcast": False
        })
        save_list(APPROVED_FILE, MICROBITS)

        send_event("refresh", "refresh")
        return jsonify({"success": True, "message": f"Microbit {id} approved."})
    return jsonify({"success": False, "message": f"Microbit {id} not found."})

#########################################################
# Reject pending microbit
#########################################################

@app.route("/reject/<id>", methods=["POST"])
def reject(id):
    if id in PENDING:
        PENDING.remove(id)
        save_list(PENDING_FILE, PENDING)
        send_event("refresh", "refresh")
        return jsonify({"success": True, "message": f"Microbit {id} rejected."})
    return jsonify({"success": False, "message": f"Microbit {id} not found."})



#########################################################
# Edeting microbits (name + location)
#########################################################

@app.route("/edit/<id>")
def edit_microbit(id):
    mb = next((m for m in MICROBITS if m["id"] == id), None)
    if not mb:
        flash("Microbit not found.", "error")
        return redirect(url_for("index"))
    return render_template("edit.html", microbit=mb)

@app.route("/edit/save", methods=["POST"])
def save_edit():
    id = request.form["id"]
    name_raw = request.form["name"]
    location_raw = request.form["location"]
    radio_raw = request.form["radio"]
    message_raw = request.form["message"]
    broadcast_raw = False

    if not ID_RE.match(id):
        flash("Invalid microbit id.")
        return redirect(url_for("index"))
    
    # --- Radio validation ---
    try:
        radio = int(radio_raw)
        if not 0 <= radio <= 255:
            flash("Radio must be between 0 and 255.")
            return redirect(url_for("edit_microbit", id=id))
    except ValueError:
        flash("Radio must be a valid integer.")
        return redirect(url_for("edit_microbit", id=id))

    for m in MICROBITS:
        if m["id"] == id:
            m["name"] = html.escape(name_raw)
            m["location"] = html.escape(location_raw)
            m["radio"] = radio
            m["message"] = html.escape(message_raw)
            m["broadcast"] = False
            break

    save_list(APPROVED_FILE, MICROBITS)
    send_event("refresh", "refresh")

    flash(f"Microbit {id} updated!", "info")
    return redirect(url_for("index"))


#########################################################
# API endpoint 
#########################################################

@app.route("/api/microbits")
def api_microbits():
    # Return the stored microbits as-is (already sanitized on write and on load)
    return jsonify({
        "approved": MICROBITS,
        "pending": PENDING
    })

#########################################################

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
