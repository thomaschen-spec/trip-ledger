import base64
import json
import os
import time
import urllib.request
from datetime import date

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
            # v2 遷移：誰付的（舊資料把記帳人當付款人）
            cur.execute("ALTER TABLE receipts ADD COLUMN IF NOT EXISTS payer_id INTEGER REFERENCES members(id)")
            cur.execute("UPDATE receipts SET payer_id = member_id WHERE payer_id IS NULL")
            # v2：分帳名單（這筆由哪些人一起分）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS receipt_splits (
                    id SERIAL PRIMARY KEY,
                    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                    member_id INTEGER NOT NULL REFERENCES members(id),
                    UNIQUE (receipt_id, member_id)
                )
            """)
        conn.commit()


# ---- 日圓 → 台幣匯率（免費 API，記憶體快取 6 小時，抓不到就不顯示台幣） ----
_rate_cache = {"rate": None, "ts": 0.0}


def get_twd_rate():
    if _rate_cache["rate"] and time.time() - _rate_cache["ts"] < 6 * 3600:
        return _rate_cache["rate"]
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/JPY", timeout=6) as resp:
            data = json.load(resp)
        rate = float(data["rates"]["TWD"])
        _rate_cache["rate"] = rate
        _rate_cache["ts"] = time.time()
        return rate
    except Exception:
        return _rate_cache["rate"]  # 過期的舊值也比沒有好；從沒抓到過就是 None


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


def get_trip(cur, trip_id):
    cur.execute("SELECT id, name, budget FROM trips WHERE id = %s", (trip_id,))
    return cur.fetchone()


def get_members(cur, trip_id):
    cur.execute("SELECT id, name, avatar FROM members WHERE trip_id = %s ORDER BY id", (trip_id,))
    return cur.fetchall()


def parse_receipt_form(form):
    """手動記帳 / 收據確認 / 編輯三個表單共用的欄位解析。"""
    tax = form.get("tax") or None
    return {
        "amount": float(form["amount"]),
        "tax": float(tax) if tax else None,
        "category": form["category"],
        "payment_method": form["payment_method"],
        "txn_date": form["txn_date"],
        "store_name": form.get("store_name", "").strip(),
        "payer_id": int(form["payer_id"]),
        "split_ids": [int(x) for x in form.getlist("split_ids")],
    }


def save_splits(cur, receipt_id, split_ids):
    cur.execute("DELETE FROM receipt_splits WHERE receipt_id = %s", (receipt_id,))
    for mid in split_ids:
        cur.execute(
            "INSERT INTO receipt_splits (receipt_id, member_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (receipt_id, mid),
        )


@app.route("/")
def home():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.name, t.budget,
                       COALESCE(SUM(r.amount), 0) AS spent,
                       COUNT(r.id) AS receipt_count
                FROM trips t LEFT JOIN receipts r ON r.trip_id = t.id
                GROUP BY t.id ORDER BY t.id DESC
            """)
            trips = cur.fetchall()
            cur.execute("""
                SELECT trip_id, MIN(start_date) AS start_date, MAX(end_date) AS end_date
                FROM itinerary GROUP BY trip_id
            """)
            dates = {d["trip_id"]: d for d in cur.fetchall()}
            cur.execute("SELECT trip_id, avatar FROM members ORDER BY id")
            avatars = {}
            for m in cur.fetchall():
                avatars.setdefault(m["trip_id"], []).append(m["avatar"])
    for t in trips:
        t["dates"] = dates.get(t["id"])
        t["avatars"] = avatars.get(t["id"], [])
        t["pct"] = min(round(t["spent"] / t["budget"] * 100), 100) if t["budget"] else None
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
            trip = get_trip(cur, trip_id)
            cur.execute(
                "SELECT id, start_date, end_date, region FROM itinerary WHERE trip_id = %s ORDER BY start_date",
                (trip_id,),
            )
            segments = cur.fetchall()
    return render_template("itinerary.html", trip=trip, segments=segments,
                           trip_id_nav=trip_id if current_member_id(trip_id) else None)


@app.route("/trip/<int:trip_id>/member", methods=["GET", "POST"])
def pick_member(trip_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            trip = get_trip(cur, trip_id)

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
            members = get_members(cur, trip_id)
    return render_template("pick_member.html", trip=trip, members=members, avatars=AVATARS,
                           trip_id_nav=trip_id if current_member_id(trip_id) else None,
                           nav_active="member")


@app.route("/trip/<int:trip_id>/dashboard")
def dashboard(trip_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            trip = get_trip(cur, trip_id)

            cur.execute(
                "SELECT COALESCE(SUM(amount),0) AS total FROM receipts WHERE trip_id = %s",
                (trip_id,),
            )
            total_spent = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT r.id, r.store_name, r.amount, r.category, r.payment_method, r.txn_date,
                       r.region, m.name AS member_name, m.avatar,
                       p.name AS payer_name, p.avatar AS payer_avatar,
                       (SELECT COUNT(*) FROM receipt_splits s WHERE s.receipt_id = r.id) AS split_count,
                       (r.image IS NOT NULL) AS has_image
                FROM receipts r
                JOIN members m ON m.id = r.member_id
                LEFT JOIN members p ON p.id = COALESCE(r.payer_id, r.member_id)
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
        rate=get_twd_rate(),
        receipts=receipts,
        daily_labels=[d["txn_date"].isoformat() for d in daily],
        daily_values=[d["total"] for d in daily],
        cat_labels=[c["category"] for c in by_category],
        cat_values=[c["total"] for c in by_category],
        pay_labels=[p["payment_method"] for p in by_payment],
        pay_values=[p["total"] for p in by_payment],
        top10=top10,
        trip_id_nav=trip_id,
        nav_active="dashboard",
    )


@app.route("/trip/<int:trip_id>/add", methods=["GET", "POST"])
def add_receipt(trip_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    if request.method == "POST":
        with get_conn() as conn:
            with conn.cursor() as cur:
                members = get_members(cur, trip_id)

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
                members=members,
                current_member_id=member_id,
                image_b64=base64.b64encode(image_bytes).decode("ascii"),
                image_mime=photo.mimetype,
                trip_id_nav=trip_id,
                nav_active="add",
            )

        f = parse_receipt_form(request.form)
        raw_text = request.form.get("raw_text", "")
        translated_text = request.form.get("translated_text", "")

        image_b64 = request.form.get("image_b64")
        image_mime = request.form.get("image_mime")
        image_bytes = base64.b64decode(image_b64) if image_b64 else None

        region = find_region(trip_id, f["txn_date"])

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO receipts
                        (trip_id, member_id, payer_id, image, image_mime, store_name, amount, tax,
                         category, payment_method, txn_date, region, raw_text, translated_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (trip_id, member_id, f["payer_id"], image_bytes, image_mime, f["store_name"],
                     f["amount"], f["tax"], f["category"], f["payment_method"], f["txn_date"],
                     region, raw_text, translated_text),
                )
                receipt_id = cur.fetchone()["id"]
                save_splits(cur, receipt_id, f["split_ids"])
            conn.commit()
        return redirect(url_for("dashboard", trip_id=trip_id))

    return render_template(
        "add_receipt.html",
        trip_id=trip_id,
        trip_id_nav=trip_id,
        nav_active="add",
    )


@app.route("/trip/<int:trip_id>/manual", methods=["GET", "POST"])
def manual_entry(trip_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    if request.method == "POST":
        f = parse_receipt_form(request.form)
        region = find_region(trip_id, f["txn_date"])
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO receipts
                        (trip_id, member_id, payer_id, store_name, amount, tax,
                         category, payment_method, txn_date, region)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (trip_id, member_id, f["payer_id"], f["store_name"], f["amount"], f["tax"],
                     f["category"], f["payment_method"], f["txn_date"], region),
                )
                receipt_id = cur.fetchone()["id"]
                save_splits(cur, receipt_id, f["split_ids"])
            conn.commit()
        return redirect(url_for("dashboard", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            members = get_members(cur, trip_id)
    return render_template(
        "receipt_form.html",
        mode="manual",
        trip_id=trip_id,
        receipt=None,
        split_ids=[m["id"] for m in members],  # 預設全員均分
        today=date.today().isoformat(),
        categories=CATEGORIES,
        payment_methods=PAYMENT_METHODS,
        members=members,
        current_member_id=member_id,
        trip_id_nav=trip_id,
        nav_active="manual",
    )


@app.route("/trip/<int:trip_id>/receipt/<int:receipt_id>/edit", methods=["GET", "POST"])
def edit_receipt(trip_id, receipt_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    if request.method == "POST":
        f = parse_receipt_form(request.form)
        region = find_region(trip_id, f["txn_date"])
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE receipts SET store_name = %s, amount = %s, tax = %s, category = %s,
                        payment_method = %s, txn_date = %s, region = %s, payer_id = %s
                    WHERE id = %s AND trip_id = %s
                    """,
                    (f["store_name"], f["amount"], f["tax"], f["category"], f["payment_method"],
                     f["txn_date"], region, f["payer_id"], receipt_id, trip_id),
                )
                save_splits(cur, receipt_id, f["split_ids"])
            conn.commit()
        return redirect(url_for("dashboard", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM receipts WHERE id = %s AND trip_id = %s", (receipt_id, trip_id)
            )
            receipt = cur.fetchone()
            if not receipt:
                return redirect(url_for("dashboard", trip_id=trip_id))
            cur.execute("SELECT member_id FROM receipt_splits WHERE receipt_id = %s", (receipt_id,))
            split_ids = [r["member_id"] for r in cur.fetchall()]
            members = get_members(cur, trip_id)
    return render_template(
        "receipt_form.html",
        mode="edit",
        trip_id=trip_id,
        receipt=receipt,
        split_ids=split_ids,
        today=receipt["txn_date"].isoformat(),
        categories=CATEGORIES,
        payment_methods=PAYMENT_METHODS,
        members=members,
        current_member_id=receipt["payer_id"] or receipt["member_id"],
        trip_id_nav=trip_id,
        nav_active="dashboard",
    )


@app.route("/trip/<int:trip_id>/settle")
def settle(trip_id):
    member_id = current_member_id(trip_id)
    if not member_id:
        return redirect(url_for("pick_member", trip_id=trip_id))

    with get_conn() as conn:
        with conn.cursor() as cur:
            trip = get_trip(cur, trip_id)
            members = get_members(cur, trip_id)
            cur.execute(
                "SELECT id, amount, COALESCE(payer_id, member_id) AS payer FROM receipts WHERE trip_id = %s",
                (trip_id,),
            )
            receipts = cur.fetchall()
            cur.execute(
                """
                SELECT s.receipt_id, s.member_id FROM receipt_splits s
                JOIN receipts r ON r.id = s.receipt_id WHERE r.trip_id = %s
                """,
                (trip_id,),
            )
            splits = {}
            for row in cur.fetchall():
                splits.setdefault(row["receipt_id"], []).append(row["member_id"])

    minfo = {m["id"]: m for m in members}
    paid = {m["id"]: 0.0 for m in members}    # 實際掏錢
    share = {m["id"]: 0.0 for m in members}   # 應該分攤
    for r in receipts:
        payer = r["payer"]
        if payer not in paid:
            continue
        paid[payer] += r["amount"]
        split_members = [m for m in splits.get(r["id"], []) if m in share]
        if split_members:
            each = r["amount"] / len(split_members)
            for m in split_members:
                share[m] += each
        else:
            share[payer] += r["amount"]  # 沒設定分帳 → 當付款人自己的花費

    balances = {mid: paid[mid] - share[mid] for mid in paid}  # 正=別人欠他

    # 最少轉帳次數的貪婪結算
    creditors = sorted(((mid, b) for mid, b in balances.items() if b > 0.5), key=lambda x: -x[1])
    debtors = sorted(((mid, -b) for mid, b in balances.items() if b < -0.5), key=lambda x: -x[1])
    transfers = []
    ci, di = 0, 0
    creditors = [list(c) for c in creditors]
    debtors = [list(d) for d in debtors]
    while ci < len(creditors) and di < len(debtors):
        give = min(creditors[ci][1], debtors[di][1])
        transfers.append({"from": minfo[debtors[di][0]], "to": minfo[creditors[ci][0]], "amount": give})
        creditors[ci][1] -= give
        debtors[di][1] -= give
        if creditors[ci][1] < 0.5:
            ci += 1
        if debtors[di][1] < 0.5:
            di += 1

    rows = [
        {"m": minfo[mid], "paid": paid[mid], "share": share[mid], "balance": balances[mid]}
        for mid in paid
    ]
    rows.sort(key=lambda r: -r["balance"])

    return render_template(
        "settle.html",
        trip=trip,
        rows=rows,
        transfers=transfers,
        rate=get_twd_rate(),
        trip_id_nav=trip_id,
        nav_active="settle",
    )


@app.route("/trip/<int:trip_id>/receipt/<int:receipt_id>/delete", methods=["POST"])
def delete_receipt(trip_id, receipt_id):
    if not current_member_id(trip_id):
        return redirect(url_for("pick_member", trip_id=trip_id))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM receipt_splits WHERE receipt_id IN (SELECT id FROM receipts WHERE id = %s AND trip_id = %s)",
                (receipt_id, trip_id),
            )
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
