
import math

import pymysql
from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_conn, query, query_one
from .security import check_csrf

bp = Blueprint("auth", __name__)


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("views.overview")


def home_for(user):
    if user.get("role") == "citizen":
        return url_for("auth.citizen_home")
    return url_for("views.overview")


def login_hero():
    try:
        rows = query("""
            SELECT status, event_timestamp
            FROM (
                SELECT status, event_timestamp
                FROM collection_event
                ORDER BY event_timestamp DESC
                LIMIT 18
            ) AS recent_events
            ORDER BY event_timestamp
        """)
    except pymysql.MySQLError:
        return []

    if len(rows) < 2:
        return []

    return [
        {
            "x": round(30 + i * 460 / (len(rows) - 1), 1),
            "y": round(160 + 70 * math.sin(i * 0.9), 1),
            "status": row["status"],
        }
        for i, row in enumerate(rows)
    ]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(home_for(session["user"]))

    error = None

    if request.method == "POST":
        check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        row = query_one("""
            SELECT user_id, username, password_hash,
                   full_name, role, worker_id
            FROM app_user
            WHERE username = %(u)s
        """, {"u": username})

        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user"] = {
                "id": row["user_id"],
                "username": row["username"],
                "full_name": row["full_name"],
                "role": row["role"],
                "worker_id": row["worker_id"],
            }

            target = request.args.get("next")
            if row["role"] == "citizen":
                return redirect(url_for("auth.citizen_home"))
            return redirect(safe_next(target))

        error = "Wrong username or password."

    return render_template(
        "login.html", error=error, hero=login_hero()
    )


@bp.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user"):
        return redirect(home_for(session["user"]))

    error = None

    if request.method == "POST":
        check_csrf()

        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or len(username) > 50:
            error = "Enter a username of 1–50 characters."
        elif not full_name or len(full_name) > 100:
            error = "Enter your full name."
        elif len(password) < 8:
            error = "Password must contain at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            try:
                conn = get_conn()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO app_user
                            (username, password_hash, full_name, role, worker_id)
                        VALUES (%s, %s, %s, 'citizen', NULL)
                    """, (
                        username,
                        generate_password_hash(password),
                        full_name,
                    ))
                conn.commit()
                flash("Account created. Please sign in.", "ok")
                return redirect(url_for("auth.login"))

            except pymysql.IntegrityError:
                if "conn" in locals():
                    conn.rollback()
                error = "That username is already taken."
            except pymysql.MySQLError:
                if "conn" in locals():
                    conn.rollback()
                error = "Could not create the account. Please try again."

    return render_template("register.html", error=error)


@bp.route("/citizen")
def citizen_home():
    user = session.get("user")

    if not user:
        return redirect(url_for("auth.login"))

    if user.get("role") != "citizen":
        return redirect(url_for("views.overview"))

    complaints = query("""
        SELECT c.bin_id, c.complaint_seq_no, c.date_raised,
               c.description, c.status
        FROM complaint c
        INNER JOIN citizen_complaint cc
          ON cc.bin_id = c.bin_id
         AND cc.complaint_seq_no = c.complaint_seq_no
        WHERE cc.user_id = %(uid)s
        ORDER BY c.date_raised DESC, c.complaint_seq_no DESC
    """, {"uid": user["id"]})

    bins = query("""
        SELECT bin_id, bin_type
        FROM bin
        ORDER BY bin_id
    """)

    return render_template(
        "citizen_home.html",
        user=user,
        complaints=complaints,
        bins=bins,
    )


@bp.route("/citizen/complaints/new", methods=["POST"])
def citizen_complaint_new():
    user = session.get("user")

    if not user:
        return redirect(url_for("auth.login"))

    if user.get("role") != "citizen":
        abort(403)

    check_csrf()

    bin_id = request.form.get("bin_id", "").strip()
    description = request.form.get("description", "").strip()

    if not bin_id or len(bin_id) > 10:
        flash("Please select a valid bin.", "error")
        return redirect(url_for("auth.citizen_home"))

    if not description or len(description) > 500:
        flash("Description must contain 1–500 characters.", "error")
        return redirect(url_for("auth.citizen_home"))

    conn = get_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bin_id FROM bin WHERE bin_id = %s",
                (bin_id,),
            )
            if not cur.fetchone():
                flash("The selected bin does not exist.", "error")
                return redirect(url_for("auth.citizen_home"))

            cur.execute("""
                SELECT COALESCE(MAX(complaint_seq_no), 0) + 1 AS next_seq
                FROM complaint
                WHERE bin_id = %s
            """, (bin_id,))
            seq = cur.fetchone()["next_seq"]

            # Insert the complaint and ownership link in one transaction.
            cur.execute("""
                INSERT INTO complaint
                    (bin_id, complaint_seq_no, date_raised, description, status)
                VALUES (%s, %s, CURDATE(), %s, 'open')
            """, (bin_id, seq, description))

            cur.execute("""
                INSERT INTO citizen_complaint
                    (user_id, bin_id, complaint_seq_no)
                VALUES (%s, %s, %s)
            """, (user["id"], bin_id, seq))

        conn.commit()
        flash(f"Complaint #{seq} submitted successfully.", "ok")

    except pymysql.MySQLError:
        conn.rollback()
        flash(
            "Could not submit the complaint. Please try again.",
            "error",
        )

    return redirect(url_for("auth.citizen_home"))


@bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("auth.login"))
