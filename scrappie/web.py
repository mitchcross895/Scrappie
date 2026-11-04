import datetime
import os
from datetime import timezone

from flask import Flask, jsonify

from .log import logger, setup_logging

app = Flask(__name__)
start_time = datetime.datetime.now(timezone.utc)  # Process-local: /health reports this process's uptime

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "bot_name": "Discord Bot",
        "version": "2.0",
        "timestamp": datetime.datetime.now(timezone.utc).isoformat()
    })

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "uptime": str(datetime.datetime.now(timezone.utc) - start_time)
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

def start_flask_server():
    """Multiprocessing target, so it must stay module-level. A spawned child has no logging until this runs."""
    setup_logging()
    port = int(os.getenv("PORT", 5000))
    logger.info(f"🌐 Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
