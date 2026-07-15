import base64
import os
from datetime import date, datetime

from flask import Flask, render_template, request, redirect, url_for, session, Response
import psycopg
from psycopg.rows import dict_row

from ocr import extract_receipt

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-in-render-env")

DATABASE_URL = os.environ.get("DATABASE_URL")

CATEGORIES = ["餐飲", "交通", "住宿", "購物", "娛樂", "其他"]
PAYMENT_METHODS = ["現金", "信用卡", "電子支付", "其他"]
AVATARS = ["🐶", "🐱", "🦊", "🐼", "🐸", "🐵", "🐧", "🦁"]


def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trips (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    budget REAL NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    id SERIAL PRIMARY KEY,
                    trip_id INTEGER NOT NULL REFERENCES trips(id),
                    name TEXT NOT NULL,
                    avatar TEXT NOT NULL,
                    UNIQUE (trip_id, name)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS itinerary (
                    id SERIAL PRIMARY KEY,
                    trip_id INTEGER NOT NULL REFERENCES trips(id),
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    region TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS receipts (
                    id SERIAL PRIMARY KEY,
                    trip_id INTEGER NOT NULL REFERENCES trips(id),
                    member_id INTEGER NOT NULL REFERENCES members(id),
                    image BYTEA,
                    image_mime TEXT,
                    store_name TEXT,
                    amount REAL NOT NULL,
                    tax REAL,
                    category TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    txn_date DATE NOT NULL,
                    region TEXT,
                    raw_text TEXT,
                    translated_text TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT now()
                )
            """)
        conn.commit()


def find_region(trip_id, txn_date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT region FROM itinerary WHERE trip_id = %s AND start_date <= %s AND end_date >= %s LIMIT 1",
                (trip_id, txn_date, txn_date),
            )
            row = cur.fetchone()
            return row["region"] if row else None


def current_member_id(trip_id):
    return session.get(f"member_{trip_id}")


@app.route("/")
def home():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, budget FROM trips ORDER BY id DESC")
            trips = cur.fetchall()
    return render_template("home.html", trips=trips)


@app.route("/trip/new", methods=["POST"])
def new_trip():
    name = request.form.get("name", "").strip()
    budget = float(request.form.get("budget") or 0)
    if not name:
        return redirect(url_for("home"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trips (name, budget) VALUES (%s, %s) RETURNING id",
                (name, budget),
            )
            trip_id = cur.fetchone()["id"]
        conn.commit()
    return redirect(url_for("itinerary", trip_id=trip_id))


@app.route("/trip/<int:trip_id>/itinerary", methods=["GET", "POST"])
def itinerary(trip_id):
    if request.method == "POST":
        start = request.form["start_date"]
        end = request.form["end_date"]
        region = request.form.get("region", "").strip()
        if region:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO itinerary (trip_id, start_date, end_date, region) VALUES (%s, %s, %s, %s)",
                        (trip_id, start, end, region),
                    )
                conn.commit()
        return redirect(url_for("itinerary", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, budget FROM trips WHERE id = %s", (trip_id,))
            trip = cur.fetchone()
            cur.execute(
                "SELECT id, start_date, end_date, region FROM itinerary WHERE trip_id = %s ORDER BY start_date",
                (trip_id,),
            )
            segments = cur.fetchall()
    return render_template("itinerary.html", trip=trip, segments=segments)


@app.route("/trip/<int:trip_id>/member", methods=["GET", "POST"])
def pick_member(trip_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, budget FROM trips WHERE id = %s", (trip_id,))
            trip = cur.fetchone()

    if request.method == "POST":
        action = request.form.get("action")
        with get_conn() as conn:
            with conn.cursor() as cur:
                if action == "create":
                    name = request.form.get("name", "").strip()
                    avatar = request.form.get("avatar", AVATARS[0])
                    if name:
                        cur.execute(
                            "INSERT INTO members (trip_id, name, avatar) VALUES (%s, %s, %s) "
                            "ON CONFLICT (trip_id, name) DO NOTHING RETURNING id",
                            (trip_id, name, avatar),
                        )
                        row = cur.fetchone()
                        conn.commit()
                        if row:
                            session[f"member_{trip_id}"] = row["id"]
                elif action == "select":
                    member_id = int(request.form["member_id"])
                    session[f"member_{trip_id}"] = member_id
        return redirect(url_for("dashboard", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, avatar FROM members WHERE trip_id = %s ORDER BY id", (trip_id,))
            members = cur.fetchall()
    return render_template("pick_member.html", trip=trip, members=members, avatars=AVATARS)


@app.route("/trip/<int:trip_id>/dashboard")
def dashboard(trip_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, budget FROM trips WHERE id = %s", (trip_id,))
            trip = cur.fetchone()

            cur.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM receipts WHERE trip_id = %s",
                (trip_id,),
            )
            total_spent = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT r.id, r.store_name, r.amount, r.category, r.payment_method, r.txn_date,
                       r.region, r.translated_text, m.name AS member_name, m.avatar
                FROM receipts r JOIN members m ON m.id = r.member_id
                WHERE r.trip_id = %s
                ORDER BY r.txn_date DESC, r.id DESC
                """,
                (trip_id,),
            )
            receipts = cur.fetchall()

            cur.execute(
                """
                SELECT txn_date, SUM(amount) AS total FROM receipts
                WHERE trip_id = %s GROUP BY txn_date ORDER BY txn_date
                """,
                (trip_id,),
            )
            daily = cur.fetchall()

            cur.execute(
                """
                SELECT category, SUM(amount) AS total FROM receipts
                WHERE trip_id = %s GROUP BY category ORDER BY total DESC
                """,
                (trip_id,),
            )
            by_category = cur.fetchall()

            cur.execute(
                """
                SELECT payment_method, SUM(amount) AS total FROM receipts
                WHERE trip_id = %s GROUP BY payment_method ORDER BY total DESC
                """,
                (trip_id,),
            )
            by_payment = cur.fetchall()

            cur.execute(
                """
                SELECT store_name, amount, txn_date FROM receipts
                WHERE trip_id = %s ORDER BY amount DESC LIMIT 10
                """,
                (trip_id,),
            )
            top10 = cur.fetchall()

    budget = trip["budget"] or 0
    pct = round((total_spent / budget) * 100, 1) if budget > 0 else None

    return render_template(
        "dashboard.html",
        trip=trip,
        total_spent=total_spent,
        budget=budget,
        pct=pct,
        receipts=receipts,
        daily_labels=[d["txn_date"].isoformat() for d in daily],
        daily_values=[d["total"] for d in daily],
        cat_labels=[c["category"] for c in by_category],
        cat_values=[c["total"] for c in by_category],
        pay_labels=[p["payment_method"] for p in by_payment],
        pay_values=[p["total"] for p in by_payment],
        top10=top10,
    )


@app.route("/trip/<int:trip_id>/add", methods=["GET", "POST"])
def add_receipt(trip_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    if request.method == "POST":
        if "photo" in request.files and request.files["photo"].filename:
            photo = request.files["photo"]
            image_bytes = photo.read()
            extracted = extract_receipt(image_bytes)
            return render_template(
                "confirm_receipt.html",
                trip_id=trip_id,
                extracted=extracted,
                today=date.today().isoformat(),
                categories=CATEGORIES,
                payment_methods=PAYMENT_METHODS,
                image_b64=base64.b64encode(image_bytes).decode("ascii"),
                image_mime=photo.mimetype,
            )

        amount = float(request.form["amount"])
        tax = request.form.get("tax") or None
        tax = float(tax) if tax else None
        category = request.form["category"]
        payment_method = request.form["payment_method"]
        txn_date = request.form["txn_date"]
        store_name = request.form.get("store_name", "")
        raw_text = request.form.get("raw_text", "")
        translated_text = request.form.get("translated_text", "")

        image_b64 = request.form.get("image_b64")
        image_mime = request.form.get("image_mime")
        image_bytes = base64.b64decode(image_b64) if image_b64 else None

        region = find_region(trip_id, txn_date)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO receipts
                        (trip_id, member_id, image, image_mime, store_name, amount, tax,
                         category, payment_method, txn_date, region, raw_text, translated_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (trip_id, member_id, image_bytes, image_mime, store_name, amount, tax,
                     category, payment_method, txn_date, region, raw_text, translated_text),
                )
            conn.commit()
        return redirect(url_for("dashboard", trip_id=trip_id))

    return render_template(
        "add_receipt.html",
        trip_id=trip_id,
        today=date.today().isoformat(),
        categories=CATEGORIES,
        payment_methods=PAYMENT_METHODS,
    )


@app.route("/trip/<int:trip_id>/receipt/<int:receipt_id>/delete", methods=["POST"])
def delete_receipt(trip_id, receipt_id):
    if not current_member_id(trip_id):
        return redirect(url_for("pick_member", trip_id=trip_id))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM receipts WHERE id = %s AND trip_id = %s", (receipt_id, trip_id))
        conn.commit()
    return redirect(url_for("dashboard", trip_id=trip_id))


@app.route("/trip/<int:trip_id>/receipt/<int:receipt_id>/image")
def receipt_image(trip_id, receipt_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT image, image_mime FROM receipts WHERE id = %s AND trip_id = %s",
                (receipt_id, trip_id),
            )
            row = cur.fetchone()
    if not row or not row["image"]:
        return "", 404
    return Response(bytes(row["image"]), mimetype=row["image_mime"] or "image/jpeg")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
