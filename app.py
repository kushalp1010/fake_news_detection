import csv
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

import google.generativeai as genai
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


load_dotenv(BASE_DIR / ".env")

logger.info("Starting Fake News Detection application")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-secret-key")
app.config["DATABASE"] = str(BASE_DIR / "database.db")
app.config["GEMINI_MODEL"] = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini API configured successfully")
else:
    logger.warning("GEMINI_API_KEY not found in environment")


def get_db():
    """Open a database connection for the current request."""
    if "db" not in g:
        logger.debug("Opening database connection")
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    """Close the database connection when the request ends."""
    db = g.pop("db", None)
    if db is not None:
        logger.debug("Closing database connection")
        db.close()


def init_db():
    """Create required tables if they do not already exist."""
    logger.info("Initializing database")
    db = sqlite3.connect(app.config["DATABASE"])
    cursor = db.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            news_text TEXT NOT NULL,
            result TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )

    db.commit()
    db.close()
    logger.info("Database initialized successfully")


def login_required(route_function):
    """Allow access only to logged-in users."""

    @wraps(route_function)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return route_function(*args, **kwargs)

    return wrapper


@app.before_request
def load_logged_in_user():
    """Load the current user from the session before each request."""
    g.user = None
    user_id = session.get("user_id")

    if user_id:
        g.user = get_db().execute(
            "SELECT id, username, email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if g.user is None:
            session.clear()


def clean_news_text(text):
    """Normalize user input and remove extra whitespace."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned


def is_valid_email(email):
    """Basic email validation for beginner-friendly form checks."""
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


def build_analysis_prompt(news_text):
    return f"""
Analyze the following news text and respond only in JSON:

{{
  "result": "REAL or FAKE or UNCERTAIN",
  "confidence": number,
  "reason": "short explanation",
  "warnings": ["warning1", "warning2"]
}}

Rules:
- Return valid JSON only.
- Keep confidence between 0 and 100.
- Use concise beginner-friendly language.
- If the text is too vague or cannot be judged reliably, use "UNCERTAIN".

News:
{news_text}
""".strip()


def extract_json(text):
    """Safely extract JSON from a model response."""
    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```json\s*|^```\s*|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def normalize_analysis(data):
    """Validate and normalize the Gemini JSON result."""
    result = str(data.get("result", "UNCERTAIN")).upper().strip()
    if result not in {"REAL", "FAKE", "UNCERTAIN"}:
        result = "UNCERTAIN"

    try:
        confidence = int(float(data.get("confidence", 50)))
    except (TypeError, ValueError):
        confidence = 50
    confidence = max(0, min(100, confidence))

    reason = str(data.get("reason", "The system could not provide a clear explanation.")).strip()
    if not reason:
        reason = "The system could not provide a clear explanation."

    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    warnings = [str(item).strip() for item in warnings if str(item).strip()]

    return {
        "result": result,
        "confidence": confidence,
        "reason": reason,
        "warnings": warnings,
    }


def analyze_with_gemini(news_text):
    """Send the text to Gemini and return normalized analysis data."""
    logger.debug("Calling Gemini API for analysis")
    if not GEMINI_API_KEY:
        logger.error("Missing Gemini API key")
        raise RuntimeError("Gemini API key is missing. Add GEMINI_API_KEY to your .env file.")

    prompt = build_analysis_prompt(news_text)
    model = genai.GenerativeModel(app.config["GEMINI_MODEL"])

    try:
        logger.debug("Generating content with Gemini")
        response = model.generate_content(prompt)
        raw_text = getattr(response, "text", "") or ""
        logger.debug(f"Raw response: {raw_text[:100]}...")
        parsed = extract_json(raw_text)
        return normalize_analysis(parsed)
    except json.JSONDecodeError as error:
        logger.error(f"JSON decode error: {error}")
        raise ValueError("Gemini returned an invalid JSON response.") from error
    except Exception as error:
        logger.error(f"Gemini API error: {error}")
        raise RuntimeError(f"Could not analyze the news text: {error}") from error


def save_history(user_id, news_text, analysis):
    """Store an analysis result for a logged-in user."""
    logger.debug(f"Saving history for user {user_id}")
    try:
        get_db().execute(
            """
            INSERT INTO history (user_id, news_text, result, confidence, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                news_text,
                analysis["result"],
                analysis["confidence"],
                analysis["reason"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        get_db().commit()
        logger.info(f"History saved for user {user_id}")
        return True
    except sqlite3.DatabaseError as e:
        logger.error(f"Failed to save history: {e}")
        return False


def get_result_label(result):
    labels = {
        "REAL": "Likely Real",
        "FAKE": "Likely Fake",
        "UNCERTAIN": "Uncertain",
    }
    return labels.get(result, "Uncertain")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    logger.info("Received analyze request")
    news_text = clean_news_text(request.form.get("news_text"))

    if len(news_text) < 20:
        logger.warning("Analyze request with insufficient text length")
        flash("Please enter at least 20 characters so the system has enough text to analyze.", "warning")
        return redirect(url_for("index"))

    try:
        logger.debug(f"Analyzing text: {news_text[:50]}...")
        analysis = analyze_with_gemini(news_text)
        logger.info(f"Analysis complete: {analysis['result']} ({analysis['confidence']}% confidence)")
    except (RuntimeError, ValueError) as error:
        logger.error(f"Analysis failed: {error}")
        flash(str(error), "danger")
        return redirect(url_for("index"))

    session["last_result"] = {
        "news_text": news_text,
        "result": analysis["result"],
        "confidence": analysis["confidence"],
        "reason": analysis["reason"],
        "warnings": analysis["warnings"],
    }

    if session.get("user_id"):
        saved = save_history(session["user_id"], news_text, analysis)
        if not saved:
            flash("Analysis completed, but saving to history failed. Please try logging in again.", "warning")

    return redirect(url_for("result"))


@app.route("/result")
def result():
    result_data = session.get("last_result")
    if not result_data:
        flash("Analyze a news article first to see the result.", "warning")
        return redirect(url_for("index"))

    return render_template(
        "result.html",
        result_data=result_data,
        result_label=get_result_label(result_data["result"]),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Please fill in all signup fields.", "warning")
            return render_template("signup.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "warning")
            return render_template("signup.html")

        if not is_valid_email(email):
            flash("Please enter a valid email address.", "warning")
            return render_template("signup.html")

        db = get_db()
        existing_user = db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()

        if existing_user:
            logger.warning(f"Signup failed - user exists: {username}")
            flash("Username or email already exists. Please use a different one.", "danger")
            return render_template("signup.html")

        try:
            db.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, generate_password_hash(password)),
            )
            db.commit()
            logger.info(f"New user registered: {username}")
        except sqlite3.IntegrityError:
            logger.warning(f"Signup failed - integrity error: {username}")
            flash("Username or email already exists. Please use a different one.", "danger")
            return render_template("signup.html")

        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_or_email = request.form.get("username_or_email", "").strip()
        password = request.form.get("password", "")

        if not username_or_email or not password:
            flash("Please enter both login fields.", "warning")
            return render_template("login.html")

        user = get_db().execute(
            """
            SELECT * FROM users
            WHERE username = ? OR email = ?
            """,
            (username_or_email, username_or_email.lower()),
        ).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            logger.warning(f"Login failed for: {username_or_email}")
            flash("Invalid username/email or password.", "danger")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        logger.info(f"User logged in: {user['username']}")
        flash("Login successful.", "success")
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    user_id = session.get("user_id")
    session.clear()
    if user_id:
        logger.info(f"User logged out: {user_id}")
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/history")
@login_required
def history():
    logger.debug("Loading history page")
    history_items = get_db().execute(
        """
        SELECT id, news_text, result, confidence, reason, created_at
        FROM history
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    logger.info(f"Loaded {len(history_items)} history items for user {session['user_id']}")
    return render_template("history.html", history_items=history_items)


@app.route("/history/export")
@login_required
def export_history():
    history_items = get_db().execute(
        """
        SELECT news_text, result, confidence, reason, created_at
        FROM history
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],),
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["News Text", "Result", "Confidence", "Reason", "Created At"])

    for item in history_items:
        writer.writerow(
            [
                item["news_text"],
                get_result_label(item["result"]),
                item["confidence"],
                item["reason"],
                item["created_at"],
            ]
        )

    memory_file = io.BytesIO()
    memory_file.write(output.getvalue().encode("utf-8-sig"))
    memory_file.seek(0)

    return send_file(
        memory_file,
        as_attachment=True,
        download_name="analysis_history.csv",
        mimetype="text/csv",
    )


@app.route("/history/clear", methods=["POST"])
@login_required
def clear_history():
    logger.info(f"Clearing history for user {session['user_id']}")
    get_db().execute("DELETE FROM history WHERE user_id = ?", (session["user_id"],))
    get_db().commit()
    flash("Your history has been cleared.", "info")
    return redirect(url_for("history"))


@app.route("/about")
def about():
    return render_template("about.html")


with app.app_context():
    init_db()


if __name__ == "__main__":
    logger.info("Starting Flask development server")
    app.run(debug=True)
