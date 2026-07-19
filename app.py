# File-purpose comment: Main Flask application file. Defines routes, helpers, database setup, and cart backend.
from flask import Flask, render_template, request, redirect, jsonify, abort, session, url_for
import hashlib
import os
import secrets
import sqlite3
import base64
import re
import smtplib
from email.message import EmailMessage
from functools import wraps
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"\'')
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-before-production")

DATABASE = "database.db"

# Directory where uploaded product images will be stored (inside project's static/images)
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'images')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Allowed image extensions for uploads
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif'}


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def cart_session_id():
    """Use a persistent cart for signed-in customers."""
    if session.get("user_id"):
        return f"user:{session['user_id']}"
    if "cart_session_id" not in session:
        session["cart_session_id"] = secrets.token_urlsafe(24)
    return session["cart_session_id"]


def logged_in_user():
    return session.get("user_id")


def login_required_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not logged_in_user():
            referrer = urlparse(request.referrer or "")
            next_path = referrer.path if referrer.path.startswith("/") else "/"
            return jsonify({"error": "Please log in before adding products to your cart", "login_url": url_for("login", next=next_path)}), 401
        return view(*args, **kwargs)
    return wrapped


def safe_next_url(value):
    if not value or urlparse(value).netloc or not value.startswith("/"):
        return url_for("home")
    return value


def send_otp_sms(mobile, otp):
    """Send an OTP through Twilio when its environment credentials are configured."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not all((account_sid, auth_token, from_number)):
        return False, "SMS service is not configured"
    payload = urlencode({"To": f"+91{mobile}", "From": from_number, "Body": f"Your ShopZone verification OTP is {otp}. It expires in 5 minutes. Do not share it with anyone."}).encode()
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    request_obj = Request(f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json", data=payload, headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urlopen(request_obj, timeout=12):
            return True, ""
    except (HTTPError, URLError, TimeoutError) as error:
        return False, f"SMS could not be sent: {error}"


def send_contact_email(name, email, mobile, address, subject, description):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER") or os.environ.get("EMAIL_USER")
    smtp_pass = os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_PASS")
    if not smtp_user or not smtp_pass:
        raise RuntimeError("SMTP credentials are not configured. Please set SMTP_USER and SMTP_PASS environment variables.")

    recipient = os.environ.get("CONTACT_RECIPIENT", "shivanigupta9847@gmail.com")
    msg = EmailMessage()
    msg["Subject"] = subject or f"New contact query from {name}"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Mobile: {mobile or 'N/A'}\n"
        f"Address: {address or 'N/A'}\n"
        f"Subject: {subject or 'N/A'}\n\n"
        f"Query:\n{description}\n"
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)


def save_contact_query(name, email, mobile, address, subject, description):
    log_path = os.path.join(app.root_path, "contact_queries.log")
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write("---\n")
        log_file.write(f"Timestamp: {datetime.utcnow().isoformat()} UTC\n")
        log_file.write(f"Name: {name}\n")
        log_file.write(f"Email: {email}\n")
        log_file.write(f"Mobile: {mobile or 'N/A'}\n")
        log_file.write(f"Address: {address or 'N/A'}\n")
        log_file.write(f"Subject: {subject or 'N/A'}\n")
        log_file.write(f"Description:\n{description}\n")
        log_file.write("---\n\n")

# Database create
def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        mobile TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS checkout_otps(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile TEXT NOT NULL,
        otp_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        mobile TEXT NOT NULL,
        address TEXT NOT NULL,
        city TEXT NOT NULL,
        pincode TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        total INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'Placed',
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        price INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS party_products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price INTEGER NOT NULL CHECK(price >= 0),
        image TEXT NOT NULL DEFAULT 'party.jpg',
        description TEXT DEFAULT '',
        stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
        active INTEGER NOT NULL DEFAULT 1
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    product_columns = {row[1] for row in cur.execute("PRAGMA table_info(party_products)").fetchall()}
    if "category" not in product_columns:
        cur.execute("ALTER TABLE party_products ADD COLUMN category TEXT NOT NULL DEFAULT 'party'")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS cart_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
        UNIQUE(session_id, product_id),
        FOREIGN KEY(product_id) REFERENCES party_products(id)
    )
    """)

    # Seed only on a fresh party catalogue. These records can be managed via API.
    if cur.execute("SELECT COUNT(*) FROM party_products").fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO party_products(name, price, image, description, stock) VALUES(?,?,?,?,?)",
            [
                ("Sequin Party Dress", 2499, "party.jpg", "Sparkle dress for evenings", 12),
                ("Designer Gown", 3999, "party.jpg", "Elegant floor-length gown", 8),
                ("Evening Wear Dress", 2999, "party.jpg", "Classic party-ready silhouette", 15),
                ("Party Lehenga", 4999, "party.jpg", "Festive embroidered lehenga", 6),
                ("Stylish Party Wear", 1999, "party.jpg", "Contemporary statement outfit", 20),
            ],
        )

    catalogue = [
        ("accessories", "Stylish Hand Bag", 1499, "accessories.jpg"), ("accessories", "Designer Watch", 1999, "watch.jpg"), ("accessories", "Fashion Sunglasses", 799, "smartwatch.jpg"), ("accessories", "Jewellery Set", 1299, "accessories.jpg"), ("accessories", "Leather Wallet", 999, "leatherbelt.jpg"),
        ("bags_and_wallets", "Leather Hand Bag", 1999, "bagsandbags.jpg"), ("bags_and_wallets", "Designer Shoulder Bag", 2499, "bagsandbags.jpg"), ("bags_and_wallets", "Travel Backpack", 1799, "bagsandbags.jpg"), ("bags_and_wallets", "Premium Wallet", 999, "leatherbelt.jpg"), ("bags_and_wallets", "Crossbody Bag", 1599, "bagsandbags.jpg"),
        ("gaming_mouse", "RGB Gaming Mouse", 999, "gamingmouse.jpg"), ("gaming_mouse", "Wireless Gaming Mouse", 1499, "gamingmouse.jpg"), ("gaming_mouse", "Pro Gamer Mouse", 1999, "gamingmouse.jpg"), ("gaming_mouse", "Mechanical Gaming Mouse", 2499, "gamingmouse.jpg"), ("gaming_mouse", "RGB Pro Mouse", 1799, "gamingmouse.jpg"),
        ("footwear", "Running Shoes", 1999, "footwear.jpg"), ("footwear", "Casual Sneakers", 1499, "sneaker.jpg"), ("footwear", "Formal Shoes", 2499, "sportshoes.jpg"), ("footwear", "Sports Shoes", 2199, "runningshoes.jpg"), ("footwear", "Leather Boots", 2799, "footwear.jpg"),
        ("kids_fashion", "Kids Party Dress", 1299, "kids.jpg"), ("kids_fashion", "Kids Casual Set", 799, "kids.jpg"), ("kids_fashion", "Baby Frock", 999, "kids.jpg"), ("kids_fashion", "Kids Traditional Wear", 1499, "kids.jpg"), ("kids_fashion", "Kids Denim Set", 1199, "kids.jpg"),
        ("women", "Women Dress", 1299, "womens.jpg"), ("women", "Party Wear Dress", 1999, "westren.jpg"), ("women", "Kurti Collection", 899, "womenethenic.jpg"), ("women", "Women Saree", 2499, "south.jpg"),
        ("north_indian", "Anarkali Suit", 2499, "north.jpg"), ("north_indian", "Punjabi Suit", 1799, "north.jpg"), ("north_indian", "Wedding Lehenga", 5999, "party.jpg"), ("north_indian", "Designer Saree", 2999, "south.jpg"),
        ("south_indian", "Kanchipuram Saree", 3499, "south.jpg"), ("south_indian", "Silk Lehenga", 4299, "south.jpg"), ("south_indian", "Traditional Kurta Set", 1799, "south.jpg"),
        ("women_ethnic", "Embroidered Kurti", 1499, "womenethenic.jpg"), ("women_ethnic", "Festive Anarkali", 2799, "womenethenic.jpg"), ("women_ethnic", "Ethnic Gown", 3299, "party.jpg"),
        ("north_eastern", "Mekhela Chador", 2899, "northeastern.jpg"), ("north_eastern", "Naga Shawl Set", 2199, "northeastern.jpg"), ("north_eastern", "Mizo Dress", 2499, "northeastern.jpg"),
        ("men", "Men Shirt", 999, "formalshirts.jpg"), ("men", "Casual Shirt", 1199, "Causal.jpg"), ("men", "Denim Jacket", 1999, "denimshirt.jpg"), ("men", "Blue Jeans", 1499, "mens.jpg"),
    ]
    for category, name, price, image in catalogue:
        exists = cur.execute("SELECT id FROM party_products WHERE name = ? AND category = ?", (name, category)).fetchone()
        if not exists:
            cur.execute("INSERT INTO party_products(name, price, image, description, stock, category) VALUES(?,?,?,?,?,?)", (name, price, image, f"Premium {name}", 20, category))

    conn.commit()
    conn.close()

init_db()


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/contact", methods=["GET", "POST"])
def contact():
    error = None
    success = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        mobile = request.form.get("mobile", "").strip()
        address = request.form.get("address", "").strip()
        subject = request.form.get("subject", "").strip()
        description = request.form.get("description", "").strip()
        if not name or not email or not description:
            error = "Please enter your name, email, and your query description."
        else:
            try:
                save_contact_query(name, email, mobile, address, subject, description)
                send_contact_email(name, email, mobile, address, subject, description)
                success = "Thank you for your message. Your query has been sent successfully."
            except Exception as exc:
                save_contact_query(name, email, mobile, address, subject, description)
                error = "Unable to send your query by email right now. Your message is saved and we will check it manually."
                app.logger.error(f"Contact form email send failed: {exc}")
    return render_template("contact.html", error=error, success=success)

#Cart
@app.route("/cart")
def cart():
    if not logged_in_user():
        return redirect(url_for("login", next="/cart"))
    return render_template("cart.html")
#shopbycategory
@app.route("/shopbycategory")
def shop():
    return render_template("shopbycategory.html")
#party_collection
@app.route("/party")
def party():
    return collection_page("party", "Party Wear Collection", "#6a0572")


def collection_page(category, title, accent):
    conn = get_db()
    products = conn.execute("SELECT * FROM party_products WHERE active = 1 AND category = ? ORDER BY id DESC", (category,)).fetchall()
    conn.close()
    return render_template("collection.html", products=products, title=title, accent=accent, user_logged_in=bool(logged_in_user()))


# Party wear catalogue API
@app.route("/api/party/products", methods=["GET"])
def get_party_products():
    conn = get_db()
    products = conn.execute(
        "SELECT * FROM party_products WHERE active = 1 ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(product) for product in products])


@app.route("/api/party/products", methods=["POST"])
def create_party_product():
    data = request.get_json(silent=True) or request.form
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Product name is required"}), 400
    try:
        price = int(data.get("price"))
        stock = int(data.get("stock", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Price and stock must be whole numbers"}), 400
    if price < 0 or stock < 0:
        return jsonify({"error": "Price and stock cannot be negative"}), 400

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO party_products(name, price, image, description, stock) VALUES(?,?,?,?,?)",
        (name, price, str(data.get("image", "party.jpg")), str(data.get("description", "")), stock),
    )
    conn.commit()
    product = conn.execute("SELECT * FROM party_products WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(product)), 201


@app.route("/api/party/cart", methods=["GET"])
@login_required_api
def get_party_cart():
    session_id = cart_session_id()
    conn = get_db()
    rows = conn.execute("""
        SELECT c.product_id, c.quantity, p.name, p.price, p.image,
               (c.quantity * p.price) AS subtotal
        FROM cart_items c JOIN party_products p ON p.id = c.product_id
        WHERE c.session_id = ?
    """, (session_id,)).fetchall()
    conn.close()
    items = [dict(row) for row in rows]
    return jsonify({"items": items, "total": sum(item["subtotal"] for item in items)})


@app.route("/api/party/cart", methods=["POST"])
@login_required_api
def add_to_party_cart():
    data = request.get_json(silent=True) or request.form
    session_id = cart_session_id()
    try:
        product_id, quantity = int(data.get("product_id")), int(data.get("quantity", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "product_id and quantity must be valid numbers"}), 400
    if quantity < 1:
        return jsonify({"error": "Quantity must be at least 1"}), 400

    conn = get_db()
    product = conn.execute(
        "SELECT id, stock FROM party_products WHERE id = ? AND active = 1", (product_id,)
    ).fetchone()
    if not product:
        conn.close()
        abort(404, description="Party product not found")
    existing = conn.execute(
        "SELECT quantity FROM cart_items WHERE session_id = ? AND product_id = ?", (session_id, product_id)
    ).fetchone()
    new_quantity = quantity + (existing["quantity"] if existing else 0)
    if new_quantity > product["stock"]:
        conn.close()
        return jsonify({"error": "Requested quantity is not available"}), 409
    conn.execute("""
        INSERT INTO cart_items(session_id, product_id, quantity) VALUES(?,?,?)
        ON CONFLICT(session_id, product_id) DO UPDATE SET quantity = excluded.quantity
    """, (session_id, product_id, new_quantity))
    conn.commit()
    conn.close()
    return jsonify({"message": "Added to cart", "product_id": product_id, "quantity": new_quantity}), 201


@app.route("/api/party/cart/<int:product_id>", methods=["PATCH", "DELETE"])
@login_required_api
def update_party_cart(product_id):
    session_id = cart_session_id()
    conn = get_db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM cart_items WHERE session_id = ? AND product_id = ?", (session_id, product_id))
        conn.commit()
        conn.close()
        return jsonify({"message": "Removed from cart"})

    data = request.get_json(silent=True) or {}
    try:
        quantity = int(data.get("quantity"))
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"error": "Quantity must be a number"}), 400
    if quantity < 1:
        conn.close()
        return jsonify({"error": "Quantity must be at least 1"}), 400
    product = conn.execute("SELECT stock FROM party_products WHERE id = ? AND active = 1", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Product not found"}), 404
    if quantity > product["stock"]:
        conn.close()
        return jsonify({"error": "Requested quantity is not available"}), 409
    cur = conn.execute("UPDATE cart_items SET quantity = ? WHERE session_id = ? AND product_id = ?", (quantity, session_id, product_id))
    conn.commit()
    conn.close()
    if not cur.rowcount:
        return jsonify({"error": "Item is not in your cart"}), 404
    return jsonify({"message": "Cart updated"})


@app.route("/api/auth/request-otp", methods=["POST"])
@login_required_api
def request_otp():
    data = request.get_json(silent=True) or {}
    mobile = str(data.get("mobile", "")).strip()
    if not mobile.isdigit() or len(mobile) != 10 or mobile[0] not in "6789":
        return jsonify({"error": "Enter a valid 10-digit Indian mobile number"}), 400
    otp = f"{secrets.randbelow(1000000):06d}"
    sent, error = send_otp_sms(mobile, otp)
    if not sent and not app.debug:
        return jsonify({"error": error}), 503
    now = datetime.utcnow()
    conn = get_db()
    conn.execute("UPDATE checkout_otps SET verified = 1 WHERE mobile = ? AND verified = 0", (mobile,))
    conn.execute("INSERT INTO checkout_otps(mobile, otp_hash, expires_at, created_at) VALUES(?,?,?,?)",
                 (mobile, hashlib.sha256(otp.encode()).hexdigest(), (now + timedelta(minutes=5)).isoformat(), now.isoformat()))
    conn.commit()
    conn.close()
    response = {"message": "OTP has been sent to your mobile number. It expires in 5 minutes."}
    if app.debug and not sent:
        response["development_otp"] = otp
    return jsonify(response)


@app.after_request
def attach_store_navigation(response):
    """Put the same responsive ecommerce navigation on every rendered page."""
    if response.content_type.startswith("text/html") and response.status_code < 400:
        page = response.get_data(as_text=True)
        if "shop-nav" not in page and "<body" in page:
            navigation = render_template("_navbar.html", logged_in=bool(logged_in_user()), user_name=session.get("user_name", ""))
            page = page.replace("</head>", '<link rel="stylesheet" href="/static/CSS/store-nav.css"></head>', 1)
            page = re.sub(r"(<body\b[^>]*>)", r"\1" + navigation, page, count=1, flags=re.IGNORECASE)
            page = page.replace("</body>", '<script src="/static/js/store-nav.js"></script></body>', 1)
            response.set_data(page)
            response.headers["Content-Length"] = len(response.get_data())
    return response


@app.route("/api/auth/verify-otp", methods=["POST"])
@login_required_api
def verify_otp():
    data = request.get_json(silent=True) or {}
    mobile, otp = str(data.get("mobile", "")).strip(), str(data.get("otp", "")).strip()
    conn = get_db()
    record = conn.execute("SELECT * FROM checkout_otps WHERE mobile = ? AND verified = 0 ORDER BY id DESC LIMIT 1", (mobile,)).fetchone()
    if not record or datetime.fromisoformat(record["expires_at"]) < datetime.utcnow() or record["otp_hash"] != hashlib.sha256(otp.encode()).hexdigest():
        conn.close()
        return jsonify({"error": "Invalid or expired OTP"}), 400
    conn.execute("UPDATE checkout_otps SET verified = 1 WHERE id = ?", (record["id"],))
    conn.commit()
    conn.close()
    session["verified_mobile"] = mobile
    return jsonify({"message": "Mobile number verified"})


@app.route("/api/checkout/place-order", methods=["POST"])
@login_required_api
def place_order():
    data = request.get_json(silent=True) or {}
    required = {key: str(data.get(key, "")).strip() for key in ("name", "mobile", "address", "city", "pincode", "payment_method")}
    if any(not value for value in required.values()):
        return jsonify({"error": "Please complete all delivery details"}), 400
    if len(required["name"]) < 2 or len(required["address"]) < 8 or len(required["city"]) < 2:
        return jsonify({"error": "Please enter a valid name and complete delivery address"}), 400
    if not required["mobile"].isdigit() or len(required["mobile"]) != 10:
        return jsonify({"error": "Enter a valid 10-digit mobile number"}), 400
    if required["mobile"] != session.get("verified_mobile"):
        return jsonify({"error": "Please verify this mobile number with OTP first"}), 403
    if not required["pincode"].isdigit() or len(required["pincode"]) != 6:
        return jsonify({"error": "Enter a valid 6-digit pincode"}), 400
    if required["payment_method"] not in ("cod", "upi", "card"):
        return jsonify({"error": "Choose a valid payment method"}), 400
    sid = cart_session_id()
    conn = get_db()
    items = conn.execute("""SELECT c.product_id, c.quantity, p.name, p.price, p.stock
                            FROM cart_items c JOIN party_products p ON p.id = c.product_id
                            WHERE c.session_id = ?""", (sid,)).fetchall()
    if not items:
        conn.close()
        return jsonify({"error": "Your cart is empty"}), 400
    if any(item["quantity"] > item["stock"] for item in items):
        conn.close()
        return jsonify({"error": "One or more items are no longer in stock"}), 409
    total = sum(item["price"] * item["quantity"] for item in items)
    cur = conn.execute("""INSERT INTO orders(session_id, customer_name, mobile, address, city, pincode, payment_method, total, created_at)
                          VALUES(?,?,?,?,?,?,?,?,?)""", (sid, required["name"], required["mobile"], required["address"], required["city"], required["pincode"], required["payment_method"], total, datetime.utcnow().isoformat()))
    order_id = cur.lastrowid
    for item in items:
        conn.execute("INSERT INTO order_items(order_id, product_id, product_name, price, quantity) VALUES(?,?,?,?,?)", (order_id, item["product_id"], item["name"], item["price"], item["quantity"]))
        conn.execute("UPDATE party_products SET stock = stock - ? WHERE id = ?", (item["quantity"], item["product_id"]))
    conn.execute("DELETE FROM cart_items WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Order placed", "order_id": order_id})


@app.route("/order-success/<int:order_id>")
def order_success(order_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND session_id = ?", (order_id, cart_session_id())).fetchone()
    conn.close()
    if not order:
        abort(404)
    return render_template("order_success.html", order=order)


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next_url(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db()
        account = conn.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        conn.close()
        if account and check_password_hash(account["password_hash"], password):
            session.clear()
            session["user_id"] = account["id"]
            session["user_name"] = account["name"]
            return redirect(next_url)
        return render_template("login.html", next_url=next_url, error="Email or password is incorrect.")
    return render_template("login.html", next_url=next_url)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    next_url = safe_next_url(request.values.get("next"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        if len(name) < 2 or "@" not in email or not (mobile.isdigit() and len(mobile) == 10) or len(password) < 6:
            return render_template("login.html", signup=True, next_url=next_url, error="Enter a name, valid email/mobile and password of at least 6 characters.")
        try:
            conn = get_db()
            cur = conn.execute("INSERT INTO accounts(name, email, mobile, password_hash, created_at) VALUES(?,?,?,?,?)", (name, email, mobile, generate_password_hash(password), datetime.utcnow().isoformat()))
            conn.commit()
            account_id = cur.lastrowid
            conn.close()
        except sqlite3.IntegrityError:
            conn.close()
            return render_template("login.html", signup=True, next_url=next_url, error="This email or mobile number is already registered.")
        session.clear()
        session["user_id"] = account_id
        session["user_name"] = name
        return redirect(next_url)
    return render_template("login.html", signup=True, next_url=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))
#Womens_collection
@app.route("/womens_collection")
def womens_collection():
    return collection_page("women", "Women's Collection", "#d63384")
#North_indian_collection
@app.route("/north_indian_collection")
def north_indian_collection():
    return collection_page("north_indian", "North Indian Collection", "#8b4513")
#Supplier
@app.route("/supplier")
def supplier():
    return render_template("/supplier/index.html")
#accessories
@app.route("/accessories")
def accessories():
    return collection_page("accessories", "Accessories", "#0d6efd")
#Bags_and_wallets
@app.route("/Bags_and_wallets")
def bags_and_wallets():
    return collection_page("bags_and_wallets", "Bags & Wallets", "#3e2723")
#footwear
@app.route("/footwear")
def footwear():
    return collection_page("footwear", "Footwear", "#0b3d91")
#gaming mouse
@app.route("/gaming_mouse")
def gaming_mouse():
    return collection_page("gaming_mouse", "Gaming Mouse", "#111")
#kids_fashion
@app.route("/kids_fashion")
def kids_fashion():
    return collection_page("kids_fashion", "Kids Fashion", "#ff9800")
# mens_collection
@app.route("/mens_collection")
def mens_collection():
    return collection_page("men", "Men's Collection", "#1f4e79")
#investor
@app.route("/investor")
def investor():
    return render_template("/investor/index.html")
#profile
@app.route("/profile")
def profile():
    return render_template("/profile/index.html")
#western_dress
@app.route("/western_dress")
def western_dress():
    return render_template("/western_dress/index.html")
#North_indian_wear
@app.route("/north_indian_wear")
def north_indian_wear():
    return render_template("/north_indian_wear/index.html")
#north_eastern_wear
@app.route("/north_eastern_wear")
def north_eastern_wear():
    return collection_page("north_eastern", "North Eastern Wear", "#4b3f72")
#south_indian_wear
@app.route("/south_indian_wear")
def south_indian_wear():
    return collection_page("south_indian", "South Indian Wear", "#8e244d")
#women_ethnic_wear
@app.route("/women_ethenic_wear")
def women_ethenic_wear():
    return collection_page("women_ethnic", "Women's Ethnic Wear", "#b23a48")
#men_ethenic _wear
@app.route("/men_ethenic_wear")
def men_ethenic_wear():
    return render_template("/men_ethenic_wear/index.html")
                         

@app.route("/add", methods=["POST"])
def add_user():
    name = request.form["name"]
    email = request.form["email"]

    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO users(name,email) VALUES(?,?)",
        (name, email) 
    )

    conn.commit()
    conn.close()

    return redirect("/users")


@app.route("/users")
def users():
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    cur.execute("SELECT * FROM users")
    data = cur.fetchall()

    conn.close()

    return render_template("users.html", users=data)


# ---- Admin routes and management ----
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin")
def admin():
    if session.get("is_admin"):
        return redirect(url_for("admin_panel"))
    return render_template("admin_login.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    next_url = safe_next_url(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db()
        admin = conn.execute("SELECT * FROM admins WHERE email = ?", (email,)).fetchone()
        conn.close()
        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["is_admin"] = True
            session["admin_id"] = admin["id"]
            session["admin_name"] = admin["name"]
            return redirect(next_url)
        return render_template("admin_login.html", error="Email or password is incorrect.")
    return render_template("admin_login.html")


@app.route("/admin/signup", methods=["GET", "POST"])
def admin_signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if len(name) < 2 or "@" not in email or len(password) < 6:
            return render_template("admin_login.html", signup=True, error="Enter a name, valid email and password at least 6 characters.")
        try:
            conn = get_db()
            cur = conn.execute("INSERT INTO admins(name, email, password_hash, created_at) VALUES(?,?,?,?)",
                               (name, email, generate_password_hash(password), datetime.utcnow().isoformat()))
            conn.commit()
            admin_id = cur.lastrowid
            conn.close()
        except sqlite3.IntegrityError:
            conn.close()
            return render_template("admin_login.html", signup=True, error="This email is already registered.")
        session.clear()
        session["is_admin"] = True
        session["admin_id"] = admin_id
        session["admin_name"] = name
        return redirect(url_for("admin_panel"))
    return render_template("admin_login.html", signup=True)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    session.pop("admin_id", None)
    session.pop("admin_name", None)
    return redirect(url_for("home"))


@app.route("/admin/panel")
@admin_required
def admin_panel():
    conn = get_db()
    products = conn.execute("SELECT * FROM party_products ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_panel.html", products=products)


@app.route("/admin/product/add", methods=["GET", "POST"])
@admin_required
def admin_add_product():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            price = int(request.form.get("price", 0))
            stock = int(request.form.get("stock", 0))
        except (TypeError, ValueError):
            return render_template("admin_edit_product.html", error="Price and stock must be numbers.")
        # Prefer uploaded file (field 'image_file'); fall back to text input 'image' or default
        image = request.form.get("image", "party.jpg")
        uploaded = request.files.get('image_file')
        if uploaded and uploaded.filename:
            filename = secure_filename(uploaded.filename)
            _, ext = os.path.splitext(filename.lower())
            if ext in ALLOWED_EXTENSIONS:
                unique = f"{secrets.token_urlsafe(6)}_{filename}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique)
                uploaded.save(save_path)
                image = unique
        description = request.form.get("description", "")
        category = request.form.get("category", "party")
        conn = get_db()
        cur = conn.execute("INSERT INTO party_products(name, price, image, description, stock, category, active) VALUES(?,?,?,?,?,?,?)",
                           (name, price, image, description, stock, category, 1))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_panel"))
    return render_template("admin_edit_product.html")


@app.route("/admin/product/edit/<int:product_id>", methods=["GET", "POST"])
@admin_required
def admin_edit_product(product_id):
    conn = get_db()
    product = conn.execute("SELECT * FROM party_products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            price = int(request.form.get("price", 0))
            stock = int(request.form.get("stock", 0))
        except (TypeError, ValueError):
            conn.close()
            return render_template("admin_edit_product.html", product=product, error="Price and stock must be numbers.")
        # Allow replacing image by uploading a new file (field 'image_file')
        image = request.form.get("image", product["image"]) or product["image"]
        uploaded = request.files.get('image_file')
        if uploaded and uploaded.filename:
            filename = secure_filename(uploaded.filename)
            _, ext = os.path.splitext(filename.lower())
            if ext in ALLOWED_EXTENSIONS:
                unique = f"{secrets.token_urlsafe(6)}_{filename}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique)
                uploaded.save(save_path)
                image = unique
        description = request.form.get("description", product["description"]) or product["description"]
        category = request.form.get("category", product["category"]) or product["category"]
        active = 1 if request.form.get("active") == "on" else 0
        conn.execute("UPDATE party_products SET name = ?, price = ?, image = ?, description = ?, stock = ?, category = ?, active = ? WHERE id = ?",
                     (name, price, image, description, stock, category, active, product_id))
        conn.commit()
        conn.close()
        return redirect(url_for("admin_panel"))
    conn.close()
    return render_template("admin_edit_product.html", product=product)


@app.route("/admin/product/delete/<int:product_id>", methods=["POST"])
@admin_required
def admin_delete_product(product_id):
    conn = get_db()
    conn.execute("DELETE FROM party_products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    app.run(debug=True)
