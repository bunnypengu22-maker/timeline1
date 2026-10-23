import base64
import binascii
import json
import mimetypes
import os
import sqlite3
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "pinned_messages.sqlite3"
SCHEMA = ROOT / "pinned_messages.sql"
UPLOADS = ROOT / "uploads" / "messages"
MAX_REQUEST_BYTES = 18 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def connect_database():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with connect_database() as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))


class RequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if self.is_local_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def is_local_origin(origin):
        if origin == "null":
            return True
        if not origin:
            return False
        return urlparse(origin).hostname in {"localhost", "127.0.0.1"}

    def do_OPTIONS(self):
        path = urlparse(self.path).path
        if path != "/api/messages" and not path.startswith("/api/messages/"):
            return self.send_error(404)
        origin = self.headers.get("Origin")
        if not self.is_local_origin(origin):
            return self.send_error(403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid content length") from None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request is empty or exceeds the upload limit")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request must contain valid JSON") from None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/messages":
            with connect_database() as connection:
                rows = connection.execute(
                    "SELECT id, message_date AS date, thoughts, image_path "
                    "FROM pinned_messages "
                    "ORDER BY message_date DESC, created_at DESC"
                ).fetchall()
            return self.send_json(200, [
                {
                    "id": row["id"],
                    "date": row["date"],
                    "thoughts": row["thoughts"],
                    "image_url": "http://127.0.0.1:%d/" % self.server.server_port + row["image_path"],
                }
                for row in rows
            ])

        requested = unquote(parsed.path).lstrip("/")
        if requested in {"server.py", "pinned_messages.sql", "pinned_messages.sqlite3"}:
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/messages":
            return self.send_error(404)
        try:
            payload = self.read_json()
            message_date = payload.get("date", "")
            thoughts = payload.get("thoughts", "").strip()
            image_data = payload.get("image", "")
            if not isinstance(message_date, str):
                raise ValueError("Enter a valid message date")
            datetime.strptime(message_date, "%Y-%m-%d")
            if not isinstance(thoughts, str) or not thoughts or len(thoughts) > 20000:
                raise ValueError("Thoughts must be between 1 and 20,000 characters")
            if not isinstance(image_data, str) or "," not in image_data:
                raise ValueError("Choose a valid screenshot")
            data_header, encoded_image = image_data.split(",", 1)
            mime_type = data_header.removeprefix("data:").split(";", 1)[0]
            extension = IMAGE_TYPES.get(mime_type)
            if not extension or "base64" not in data_header:
                raise ValueError("Use a JPEG, PNG, WebP, or GIF screenshot")
            try:
                image_bytes = base64.b64decode(encoded_image, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("Screenshot data is invalid") from None
            if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
                raise ValueError("Screenshot must be smaller than 12 MB")

            message_id = uuid.uuid4().hex
            image_path = UPLOADS / (message_id + extension)
            relative_path = image_path.relative_to(ROOT).as_posix()
            image_path.write_bytes(image_bytes)
            try:
                with connect_database() as connection:
                    connection.execute(
                        "INSERT INTO pinned_messages "
                        "(id, message_date, thoughts, image_path) VALUES (?, ?, ?, ?)",
                        (message_id, message_date, thoughts, relative_path),
                    )
            except Exception:
                image_path.unlink(missing_ok=True)
                raise

            return self.send_json(201, {
                "id": message_id,
                "date": message_date,
                "thoughts": thoughts,
                "image_url": "http://127.0.0.1:%d/" % self.server.server_port + relative_path,
            })
        except (ValueError, TypeError) as error:
            return self.send_json(400, {"error": str(error)})
        except Exception:
            return self.send_json(500, {"error": "Could not save the pinned message"})

    def do_DELETE(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "messages"]:
            return self.send_error(404)
        message_id = parts[2]
        with connect_database() as connection:
            row = connection.execute(
                "SELECT image_path FROM pinned_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                return self.send_json(404, {"error": "Pinned message not found"})
            connection.execute("DELETE FROM pinned_messages WHERE id = ?", (message_id,))
        image_path = (ROOT / row["image_path"]).resolve()
        if image_path.is_relative_to(UPLOADS.resolve()):
            image_path.unlink(missing_ok=True)
        return self.send_json(200, {"deleted": True})


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8765"))), RequestHandler)
    print("Pinned messages site running at http://127.0.0.1:%d" % server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()