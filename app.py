"""
Guerrero Dental — Full-Stack Flask Application (Unified v2.1)
=============================================================
Serves all HTML pages as Flask templates + full REST API.

Run:
    pip install -r requirements.txt
    python app.py

Then open:  http://localhost:5000
"""

from flask import Flask, request, jsonify, abort, render_template, redirect, url_for, send_file
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from functools import wraps
import bcrypt
import os
import smtplib
import ssl
import traceback
from email.message import EmailMessage
from io import BytesIO
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# ── SETUP ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.config["JWT_SECRET_KEY"]             = os.environ.get("JWT_SECRET", "gd-super-secret-2025-change-me-longer-safe-key!!")
app.config["JWT_ACCESS_TOKEN_EXPIRES"]   = timedelta(hours=10)
# ⚠️  For production, always set JWT_SECRET and FLASK_SECRET as environment variables!
# use a central file in the instance folder so data is persisted across restarts
# `flask run`/`python app.py` will create the `instance` directory automatically if it
# doesn't exist.  SQLAlchemy resolves relative URIs against `instance_path`, which
# meant `sqlite:///instance/guerrero_dental.db` became
# `.../instance/instance/guerrero_dental.db` (see debugging notes).  To avoid that
# and make the location explicit we build an absolute path here.  An environment
# variable may still override for tests or deployment.
#
# The triple slash (`sqlite:///`) is followed by an absolute path.  On Windows the
# leading slash is required; we let `os.path.abspath` handle platform details.
base = os.environ.get("DATABASE_URL")
if base:
    app.config["SQLALCHEMY_DATABASE_URI"] = base
else:
    # Ensure the instance folder exists (Railway has no pre-created folders)
    os.makedirs(app.instance_path, exist_ok=True)
    db_file = os.path.join(app.instance_path, "guerrero_dental.db")
    # Convert backslashes to forward slashes for SQLAlchemy URI
    db_path = os.path.abspath(db_file).replace("\\", "/")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"]                 = os.environ.get("FLASK_SECRET", "gd-flask-secret-2025")

CORS(app, origins=["*"], supports_credentials=True)
jwt = JWTManager(app)

@jwt.unauthorized_loader
def custom_unauthorized_response(err):
    return jsonify({"message": "Missing authorization header."}), 401

@jwt.invalid_token_loader
def custom_invalid_token_response(err):
    return jsonify({"message": "Invalid token."}), 401

@jwt.expired_token_loader
def custom_expired_token_response(jwt_header, jwt_payload):
    return jsonify({"message": "Token has expired."}), 401

@jwt.revoked_token_loader
def custom_revoked_token_response(jwt_header, jwt_payload):
    return jsonify({"message": "Token has been revoked."}), 401

db  = SQLAlchemy(app)


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def send_email(recipient, subject, body, sender=None):
    mail_server = os.environ.get("MAIL_SERVER")
    if not mail_server:
        raise RuntimeError("Mail server not configured. Set MAIL_SERVER environment variable.")

    port = int(os.environ.get("MAIL_PORT", "587"))
    username = os.environ.get("MAIL_USERNAME", "")
    password = os.environ.get("MAIL_PASSWORD", "")
    use_ssl = _env_bool("MAIL_USE_SSL", False)
    use_tls = _env_bool("MAIL_USE_TLS", True)
    from_addr = sender or os.environ.get("MAIL_DEFAULT_SENDER", "info@guerrerodental.com")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(mail_server, port, context=context) as smtp:
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(mail_server, port, timeout=30) as smtp:
            if use_tls:
                context = ssl.create_default_context()
                smtp.starttls(context=context)
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)


# ── PDF GENERATION HELPERS ────────────────────────────────────────────────
def _draw_doctor_signature_footer(canvas, doc):
    """Draw one doctor-signature line at page bottom-right."""
    brand_brown = colors.HexColor("#7C3D12")
    canvas.saveState()
    canvas.setStrokeColor(brand_brown)
    canvas.setFillColor(brand_brown)
    canvas.setLineWidth(1)

    line_width = 2.0 * inch
    x2 = doc.pagesize[0] - doc.rightMargin
    x1 = x2 - line_width
    y = max(20, doc.bottomMargin - 10)

    # No extra brand icon at the top of patient backup printouts.
    # Signature line above, label below.
    canvas.line(x1, y + 10, x2, y + 10)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(x1, y, "Doctor signature")
    canvas.restoreState()


def _draw_pdf_brand(canvas, doc):
    """Render Guerrero Dental logo and brand name at the top of exported PDFs."""
    canvas.saveState()
    x = doc.leftMargin
    y = doc.pagesize[1] - 0.65 * inch
    radius = 0.28 * inch

    canvas.setFillColor(colors.HexColor('#C9933C'))
    canvas.circle(x + radius, y, radius, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 10)
    canvas.drawCentredString(x + radius, y - 2, 'GD')

    canvas.setFillColor(colors.HexColor('#7C3D12'))
    canvas.setFont('Helvetica-Bold', 14)
    canvas.drawString(x + radius * 2 + 10, y - 4, 'Guerrero Dental')
    canvas.restoreState()


# ── MODELS ────────────────────────────────────────────────────────────────
class StaffUser(db.Model):
    __tablename__ = "staff_users"
    id            = db.Column(db.Integer,    primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.LargeBinary, nullable=False)
    role          = db.Column(db.String(20),  default="nurse")
    is_active     = db.Column(db.Boolean,     default=True)
    created_at    = db.Column(db.DateTime,    default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by    = db.Column(db.Integer,     db.ForeignKey("staff_users.id"), nullable=True)

    def set_password(self, pw):
        self.password_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt())

    def check_password(self, pw):
        return bcrypt.checkpw(pw.encode(), self.password_hash)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "email": self.email,
            "role": self.role, "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Patient(db.Model):
    __tablename__ = "patients"
    id          = db.Column(db.Integer,    primary_key=True)
    patient_id  = db.Column(db.String(20), unique=True)
    name        = db.Column(db.String(120), nullable=False)
    email       = db.Column(db.String(120))
    phone       = db.Column(db.String(30))
    dob         = db.Column(db.String(20))
    address     = db.Column(db.Text)
    status      = db.Column(db.String(20), default="active")
    last_visit  = db.Column(db.String(20))
    notes       = db.Column(db.Text)
    created_at  = db.Column(db.DateTime,  default=datetime.utcnow)
    appointments = db.relationship("Appointment", backref="patient", lazy=True, cascade="all,delete")

    def to_dict(self):
        return {
            "id": self.id, "patientId": self.patient_id, "name": self.name,
            "email": self.email, "phone": self.phone, "dob": self.dob,
            "address": self.address, "status": self.status,
            "lastVisit": self.last_visit, "notes": self.notes,
        }


class Appointment(db.Model):
    __tablename__ = "appointments"
    id           = db.Column(db.Integer, primary_key=True)
    patient_id   = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    patient_name = db.Column(db.String(120))
    type         = db.Column(db.String(80))
    date         = db.Column(db.String(20))
    time         = db.Column(db.String(10))
    status       = db.Column(db.String(20), default="scheduled")
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime,  default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "patientId": self.patient_id, "patientName": self.patient_name,
            "type": self.type, "date": self.date, "time": self.time,
            "status": self.status, "notes": self.notes,
        }


class Complaint(db.Model):
    __tablename__ = "complaints"
    id             = db.Column(db.Integer,    primary_key=True)
    name           = db.Column(db.String(120), nullable=False)
    phone          = db.Column(db.String(30))
    email          = db.Column(db.String(120))
    dob            = db.Column(db.String(20))
    concern        = db.Column(db.String(200))
    description    = db.Column(db.Text)
    urgency        = db.Column(db.String(20), default="routine")
    preferred_date = db.Column(db.String(20))
    preferred_time = db.Column(db.String(80))
    referral       = db.Column(db.String(80))
    status         = db.Column(db.String(20), default="new")
    assigned_to    = db.Column(db.Integer,    db.ForeignKey("staff_users.id"), nullable=True)
    assigned_doctor = db.Column(db.String(120))
    staff_notes    = db.Column(db.Text)
    created_at     = db.Column(db.DateTime,   default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime,   default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "phone": self.phone,
            "email": self.email, "dob": self.dob, "concern": self.concern,
            "description": self.description, "urgency": self.urgency,
            "preferredDate": self.preferred_date, "preferredTime": self.preferred_time,
            "referral": self.referral, "status": self.status,
            "assignedTo": self.assigned_to, "assignedDoctor": self.assigned_doctor, "staffNotes": self.staff_notes,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class Message(db.Model):
    __tablename__ = "messages"
    id         = db.Column(db.Integer,    primary_key=True)
    from_name  = db.Column(db.String(120))
    to_name    = db.Column(db.String(120))
    subject    = db.Column(db.String(200))
    body       = db.Column(db.Text)
    date       = db.Column(db.String(20))
    read       = db.Column(db.Boolean,   default=False)
    created_at = db.Column(db.DateTime,  default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "from": self.from_name, "to": self.to_name,
            "subject": self.subject, "body": self.body,
            "date": self.date, "read": self.read,
        }


# ── HELPERS ───────────────────────────────────────────────────────────────
def require_role(*roles):
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            uid  = get_jwt_identity()
            user = db.session.get(StaffUser, int(uid))
            if not user or not user.is_active:
                return jsonify({"message": "Account not found or inactive."}), 403
            if user.role not in roles:
                return jsonify({"message": f"Access denied. Required: {' or '.join(roles)}"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def get_current_user():
    uid = get_jwt_identity()
    return db.session.get(StaffUser, int(uid))


# ── PAGE ROUTES ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/gd-admin/login")
def staff_login():
    return render_template("staff_login.html")

@app.route("/gd-admin/admin-login")
def admin_login():
    return render_template("admin_login.html")

@app.route("/gd-admin/dashboard/superadmin")
def superadmin_dashboard():
    return render_template("superadmin_dashboard.html")

@app.route("/gd-admin/dashboard/nurse")
def nurse_dashboard():
    return render_template("nurse_dashboard.html")

@app.route("/gd-admin/dashboard/admin")
def admin_dashboard():
    return render_template("admin_dashboard.html")

# Legacy HTML file redirects
@app.route("/index.html")
def r_index(): return redirect(url_for("index"))
@app.route("/gd-admin/staff_login.html")
def r_staff_login(): return redirect('/gd-admin/login')
@app.route("/gd-admin/admin_login.html")
def r_admin_login(): return redirect('/gd-admin/admin-login')
@app.route("/gd-admin/superadmin_dashboard.html")
def r_superadmin(): return redirect('/gd-admin/dashboard/superadmin')
@app.route("/gd-admin/nurse_dashboard.html")
def r_nurse(): return redirect('/gd-admin/dashboard/nurse')
@app.route("/gd-admin/admin_dashboard.html")
def r_admin(): return redirect('/gd-admin/dashboard/admin')

# Common typo shortcut: /gd-adm -> /gd-admin/login
@app.route("/gd-adm")
@app.route("/gd-adm/")
def r_gd_adm():
    return redirect("/gd-admin/login")


# ── AUTH API ──────────────────────────────────────────────────────────────
@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict) or not data.get("email") or not data.get("password"):
        return jsonify({"message": "Email and password required."}), 400

    user = StaffUser.query.filter_by(email=data["email"].strip().lower()).first()
    if not user or not user.check_password(data["password"]):
        return jsonify({"message": "Invalid email or password."}), 401
    if not user.is_active:
        return jsonify({"message": "Account deactivated. Contact administrator."}), 403

    token = create_access_token(identity=str(user.id))
    return jsonify({
        "token": token, "id": user.id,
        "name": user.name, "email": user.email, "role": user.role,
    }), 200


@app.route("/api/auth/me", methods=["GET"])
@jwt_required()
def api_me():
    user = get_current_user()
    if not user:
        return jsonify({"message": "User not found."}), 404
    return jsonify(user.to_dict())


# ── STAFF USERS (superadmin only) ─────────────────────────────────────────
@app.route("/api/users", methods=["GET"])
@require_role("superadmin")
def list_users():
    return jsonify([u.to_dict() for u in StaffUser.query.order_by(StaffUser.created_at.desc()).all()])


@app.route("/api/users", methods=["POST"])
@require_role("superadmin")
def create_user():
    data    = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"message": "Request body must be valid JSON."}), 400
    creator = get_current_user()
    if not data.get("name") or not data.get("email") or not data.get("password"):
        return jsonify({"message": "Name, email, and password are required."}), 400
    role = data.get("role", "nurse")
    if role not in ("superadmin", "admin", "nurse"):
        return jsonify({"message": "Role must be superadmin, admin, or nurse."}), 400
    if len(data["password"]) < 8:
        return jsonify({"message": "Password must be at least 8 characters."}), 400
    if StaffUser.query.filter_by(email=data["email"].strip().lower()).first():
        return jsonify({"message": "Email already in use."}), 409
    is_active = bool(data.get("is_active", True))
    user = StaffUser(
        name=data["name"].strip(), email=data["email"].strip().lower(),
        role=role, is_active=is_active, created_by=creator.id
    )
    user.set_password(data["password"])
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201


@app.route("/api/users/<int:uid>", methods=["PUT"])
@require_role("superadmin")
def update_user(uid):
    user   = StaffUser.query.get_or_404(uid)
    data   = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"message": "Request body must be valid JSON."}), 400
    editor = get_current_user()
    if "name"  in data: user.name = data["name"].strip()
    if "email" in data:
        ex = StaffUser.query.filter_by(email=data["email"].strip().lower()).first()
        if ex and ex.id != uid:
            return jsonify({"message": "Email already in use."}), 409
        user.email = data["email"].strip().lower()
    if "role" in data:
        if data["role"] not in ("superadmin", "admin", "nurse"):
            return jsonify({"message": "Invalid role."}), 400
        if editor.id == uid and data["role"] != "superadmin":
            return jsonify({"message": "Cannot change your own role."}), 400
        user.role = data["role"]
    if "is_active" in data:
        if editor.id == uid:
            return jsonify({"message": "Cannot deactivate your own account."}), 400
        user.is_active = bool(data["is_active"])
    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"message": "Password must be at least 8 characters."}), 400
        user.set_password(data["password"])
    user.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(user.to_dict())


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@require_role("superadmin")
def delete_user(uid):
    user   = StaffUser.query.get_or_404(uid)
    editor = get_current_user()
    if editor.id == uid:
        return jsonify({"message": "Cannot delete your own account."}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "User deleted."}), 200



# ── USERS (ADMIN role — can only manage nurses) ───────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@require_role("superadmin", "admin")
def admin_list_users():
    u = get_current_user()
    if u.role == "superadmin":
        users = StaffUser.query.order_by(StaffUser.created_at.desc()).all()
    else:
        users = StaffUser.query.filter_by(role="nurse").order_by(StaffUser.created_at.desc()).all()
    return jsonify([x.to_dict() for x in users])

@app.route("/api/admin/users", methods=["POST"])
@require_role("superadmin", "admin")
def admin_create_user():
    data    = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"message": "Request body must be valid JSON."}), 400
    creator = get_current_user()
    if not data.get("name") or not data.get("email") or not data.get("password"):
        return jsonify({"message": "Name, email, and password are required."}), 400
    role = data.get("role", "nurse")
    # Admin can only create nurses; superadmin can create any role
    if creator.role == "admin" and role != "nurse":
        return jsonify({"message": "Admin can only create nurse accounts."}), 403
    if role not in ("superadmin", "admin", "nurse"):
        return jsonify({"message": "Invalid role."}), 400
    if len(data["password"]) < 8:
        return jsonify({"message": "Password must be at least 8 characters."}), 400
    if StaffUser.query.filter_by(email=data["email"].strip().lower()).first():
        return jsonify({"message": "Email already in use."}), 409
    user = StaffUser(
        name=data["name"].strip(), email=data["email"].strip().lower(),
        role=role, is_active=True, created_by=creator.id
    )
    user.set_password(data["password"])
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201

@app.route("/api/admin/users/<int:uid>", methods=["PUT"])
@require_role("superadmin", "admin")
def admin_update_user(uid):
    user   = StaffUser.query.get_or_404(uid)
    editor = get_current_user()
    # Admin can only edit nurses
    if editor.role == "admin" and user.role != "nurse":
        return jsonify({"message": "Admin can only edit nurse accounts."}), 403
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"message": "Request body must be valid JSON."}), 400
    if "name"  in data: user.name = data["name"].strip()
    if "email" in data:
        ex = StaffUser.query.filter_by(email=data["email"].strip().lower()).first()
        if ex and ex.id != uid:
            return jsonify({"message": "Email already in use."}), 409
        user.email = data["email"].strip().lower()
    if "role" in data and editor.role == "superadmin":
        user.role = data["role"]
    if "is_active" in data:
        if editor.id == uid:
            return jsonify({"message": "Cannot deactivate your own account."}), 400
        user.is_active = bool(data["is_active"])
    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"message": "Password must be at least 8 characters."}), 400
        user.set_password(data["password"])
    user.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(user.to_dict())

@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@require_role("superadmin", "admin")
def admin_delete_user(uid):
    user   = StaffUser.query.get_or_404(uid)
    editor = get_current_user()
    if editor.role == "admin" and user.role != "nurse":
        return jsonify({"message": "Admin can only delete nurse accounts."}), 403
    if editor.id == uid:
        return jsonify({"message": "Cannot delete your own account."}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "User deleted."}), 200


# ── COMPLAINTS ─────────────────────────────────────────────────────────────
@app.route("/api/complaints", methods=["POST"])
def submit_complaint():
    data = request.get_json()
    if not data or not data.get("name") or not data.get("phone"):
        return jsonify({"message": "Name and phone are required."}), 400
    c = Complaint(
        name=data["name"], phone=data.get("phone"), email=data.get("email"),
        dob=data.get("dob"), concern=data.get("concern"),
        description=data.get("description"), urgency=data.get("urgency","routine"),
        preferred_date=data.get("preferredDate"), preferred_time=data.get("preferredTime"),
        referral=data.get("referral"), status="new"
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({"message": "Inquiry received. We will contact you within 24 hours.", "id": c.id}), 201


@app.route("/api/complaints", methods=["GET"])
@require_role("superadmin", "admin", "nurse")
def list_complaints():
    status = request.args.get("status")
    q = Complaint.query
    if status and status != "all":
        q = q.filter_by(status=status)
    return jsonify([c.to_dict() for c in q.order_by(Complaint.created_at.desc()).all()])


@app.route("/api/complaints/<int:cid>", methods=["PUT"])
@require_role("superadmin", "admin", "nurse")
def update_complaint(cid):
    c    = Complaint.query.get_or_404(cid)
    data = request.get_json()
    if "status"     in data: c.status           = data["status"]
    if "staffNotes" in data: c.staff_notes      = data["staffNotes"]
    if "assignedTo" in data: c.assigned_to      = data["assignedTo"]
    if "assignedDoctor" in data: c.assigned_doctor = data["assignedDoctor"]
    c.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(c.to_dict())


@app.route("/api/complaints/<int:cid>", methods=["DELETE"])
@require_role("superadmin")
def delete_complaint(cid):
    c = Complaint.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    return jsonify({"message": "Complaint deleted."}), 200


# ── PATIENTS ──────────────────────────────────────────────────────────────
@app.route("/api/patients", methods=["GET"])
@require_role("superadmin", "admin", "nurse")
def list_patients():
    return jsonify([p.to_dict() for p in Patient.query.order_by(Patient.created_at.desc()).all()])


@app.route("/api/patients", methods=["POST"])
@require_role("superadmin", "admin", "nurse")
def create_patient():
    data = request.get_json()
    if not data.get("name") or not data.get("phone"):
        return jsonify({"message": "Name and phone are required."}), 400
    count = Patient.query.count() + 1
    p = Patient(
        patient_id=f"GD{1000 + count}", name=data["name"],
        email=data.get("email"), phone=data["phone"],
        dob=data.get("dob"), address=data.get("address"), status="active"
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201


@app.route("/api/patients/<int:pid>", methods=["PUT"])
@require_role("superadmin", "admin", "nurse")
def update_patient(pid):
    p    = Patient.query.get_or_404(pid)
    data = request.get_json()
    for f in ["name","email","phone","dob","address","status","notes"]:
        if f in data: setattr(p, f, data[f])
    db.session.commit()
    return jsonify(p.to_dict())


@app.route("/api/patients/<int:pid>", methods=["DELETE"])
@require_role("superadmin")
def delete_patient(pid):
    p = Patient.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"message": "Patient deleted."}), 200


# ── APPOINTMENTS ──────────────────────────────────────────────────────────
@app.route("/api/appointments", methods=["GET"])
@require_role("superadmin", "admin", "nurse")
def list_appointments():
    return jsonify([a.to_dict() for a in Appointment.query.order_by(Appointment.date.desc(), Appointment.time).all()])


@app.route("/api/appointments", methods=["POST"])
@require_role("superadmin", "admin", "nurse")
def create_appointment():
    data = request.get_json()
    p    = Patient.query.get(data.get("patientId"))
    if not p:
        return jsonify({"message": "Patient not found."}), 404
    a = Appointment(
        patient_id=p.id, patient_name=p.name,
        type=data.get("type"), date=data.get("date"),
        time=data.get("time"), status="scheduled", notes=data.get("notes","")
    )
    p.last_visit = data.get("date")
    db.session.add(a)
    db.session.commit()
    return jsonify(a.to_dict()), 201


@app.route("/api/appointments/<int:aid>", methods=["PUT"])
@require_role("superadmin", "admin", "nurse")
def update_appointment(aid):
    a    = Appointment.query.get_or_404(aid)
    data = request.get_json()
    for f in ["type","date","time","status","notes"]:
        if f in data: setattr(a, f, data[f])
    db.session.commit()
    return jsonify(a.to_dict())


@app.route("/api/appointments/<int:aid>", methods=["DELETE"])
@require_role("nurse", "superadmin")
def delete_appointment(aid):
    a = Appointment.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({"message": "Appointment deleted."}), 200


# ── MESSAGES ──────────────────────────────────────────────────────────────
@app.route("/api/messages", methods=["GET"])
@require_role("superadmin", "admin")
def list_messages():
    return jsonify([m.to_dict() for m in Message.query.order_by(Message.created_at.desc()).all()])


@app.route("/api/messages", methods=["POST"])
@require_role("superadmin", "admin")
def send_message():
    user = get_current_user()
    data = request.get_json()
    m = Message(
        from_name=user.name, to_name=data.get("to"),
        subject=data.get("subject"), body=data.get("body"),
        date=datetime.now().strftime("%Y-%m-%d"), read=True
    )
    db.session.add(m)
    db.session.commit()
    return jsonify(m.to_dict()), 201


@app.route("/api/messages/<int:mid>/read", methods=["PUT"])
@require_role("superadmin", "admin", "nurse")
def mark_read(mid):
    m = Message.query.get_or_404(mid)
    m.read = True
    db.session.commit()
    return jsonify(m.to_dict())


# ── DASHBOARD STATS ───────────────────────────────────────────────────────
@app.route("/api/dashboard/stats", methods=["GET"])
@require_role("superadmin", "admin", "nurse")
def dashboard_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    return jsonify({
        "totalPatients":        Patient.query.count(),
        "activePatients":       Patient.query.filter_by(status="active").count(),
        "todayAppointments":    Appointment.query.filter_by(date=today).count(),
        "upcomingAppointments": Appointment.query.filter(Appointment.date >= today).count(),
        "unreadMessages":       Message.query.filter_by(read=False).count(),
        "newComplaints":        Complaint.query.filter_by(status="new").count(),
        "totalComplaints":      Complaint.query.count(),
        "staffCount":           StaffUser.query.filter_by(is_active=True).count(),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Guerrero Dental API", "version": "2.1.0"}), 200


# ── PDF GENERATION HELPERS ────────────────────────────────────────────────
def generate_pdf_patients(patients_list):
    """Generate printable patient record forms with header once per page and compact cards."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.5 * inch,
        bottomMargin=0.45 * inch,
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
    )
    elements = []
    styles = getSampleStyleSheet()
    brand_brown = colors.HexColor("#7C3D12")
    brand_gold = colors.HexColor("#C9933C")
    brand_muted = colors.HexColor("#8A7A6A")
    panel_bg = colors.HexColor("#FAF6F0")
    card_width = doc.width

    heading_style = ParagraphStyle(
        "PatientFormHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=16,
        alignment=TA_LEFT,
        textColor=brand_brown,
        leading=18,
        spaceAfter=0,
    )
    subheading_style = ParagraphStyle(
        "PatientFormSubheading",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        alignment=TA_LEFT,
        textColor=brand_muted,
        leading=11,
        spaceAfter=0,
    )
    label_style = ParagraphStyle(
        "PatientFieldLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        textColor=brand_brown,
        leading=11,
    )
    value_style = ParagraphStyle(
        "PatientFieldValue",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        textColor=colors.HexColor("#2A241F"),
        leading=12,
    )

    # Create the page header with logo box
    logo_box = Table([['GD']], colWidths=[0.95 * inch], rowHeights=[0.6 * inch])
    logo_box.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, brand_brown),
        ('BACKGROUND', (0, 0), (-1, -1), brand_brown),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.white),
    ]))
    title_block = Table(
        [[Paragraph('Guerrero Dental', heading_style)], [Paragraph('patient records', subheading_style)]],
        colWidths=[card_width - 1.05 * inch],
        rowHeights=[0.32 * inch, 0.26 * inch],
    )
    title_block.setStyle(TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    page_header = Table([[logo_box, title_block]], colWidths=[1.05 * inch, card_width - 1.05 * inch], rowHeights=[0.75 * inch])
    page_header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('LINEBELOW', (0, 0), (-1, 0), 1, brand_gold),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(page_header)

    records = patients_list if patients_list else [None]
    for i, patient in enumerate(records):
        date_value = datetime.now().strftime("%Y-%m-%d")
        if patient and getattr(patient, "last_visit", None):
            date_value = patient.last_visit

        # Compact patient card without header
        date_table = Table(
            [[Paragraph('<b>Date:</b>', label_style), Paragraph(date_value, value_style)]],
            colWidths=[0.7 * inch, card_width - 0.7 * inch],
            rowHeights=[0.32 * inch],
        )
        date_table.setStyle(TableStyle([
            ('LINEBELOW', (1, 0), (1, 0), 1, brand_brown),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))

        status_val = (patient.status.capitalize() if patient and patient.status else '')
        patient_name = (patient.name if patient and patient.name else '')
        phone_val = (patient.phone if patient and patient.phone else '')
        doctor_val = (patient.assigned_doctor if patient and getattr(patient, 'assigned_doctor', None) else '')

        info_rows = [
            [Paragraph('Status:', label_style), Paragraph(status_val, value_style), Paragraph('Date:', label_style), Paragraph(date_value, value_style)],
            [Paragraph('Name:', label_style), Paragraph(patient_name, value_style), '', ''],
            [Paragraph('Phone:', label_style), Paragraph(phone_val, value_style), '', ''],
            [Paragraph('Assigned doctor:', label_style), Paragraph(doctor_val, value_style), '', ''],
        ]
        info_table = Table(
            info_rows,
            colWidths=[0.8 * inch, 3.0 * inch, 0.6 * inch, card_width - 4.4 * inch],
            rowHeights=[0.32 * inch, 0.32 * inch, 0.32 * inch, 0.34 * inch],
        )
        info_table.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 1.2, brand_brown),
            ('BACKGROUND', (0, 0), (-1, -1), panel_bg),
            ('LINEBELOW', (1, 0), (1, 0), 1, brand_brown),
            ('LINEBELOW', (3, 0), (3, 0), 1, brand_brown),
            ('LINEBELOW', (1, 1), (1, 1), 1, brand_brown),
            ('LINEBELOW', (1, 2), (1, 2), 1, brand_brown),
            ('LINEBELOW', (1, 3), (1, 3), 1, brand_brown),
            ('TEXTCOLOR', (0, 0), (0, -1), brand_brown),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))

        card_container = Table(
            [[date_table], [info_table]],
            colWidths=[card_width],
            style=TableStyle([
                ('BOX', (0, 0), (-1, -1), 1.3, brand_brown),
                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 1),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ])
        )

        elements.append(KeepTogether([card_container]))

        if (i + 1) % 3 == 0 and i + 1 < len(records):
            elements.append(PageBreak())
            # Add header again on new page
            elements.append(page_header)
        else:
            elements.append(Spacer(1, 0.05 * inch))


    def _draw_footer(canvas, doc):
        """Draw only the signature footer at page bottom."""
        _draw_doctor_signature_footer(canvas, doc)

    doc.build(elements, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    buffer.seek(0)
    return buffer


def generate_pdf_appointments(appointments_list, patients_dict=None):
    """Generate printable appointment record forms based on clinic layout."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.8 * inch,
        bottomMargin=0.45 * inch,
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
    )
    elements = []
    styles = getSampleStyleSheet()
    brand_brown = colors.HexColor("#7C3D12")
    brand_gold = colors.HexColor("#C9933C")
    brand_muted = colors.HexColor("#8A7A6A")
    panel_bg = colors.HexColor("#FAF6F0")

    heading_style = ParagraphStyle(
        "ApptFormHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=19,
        alignment=TA_LEFT,
        textColor=brand_brown,
        leading=22,
        spaceAfter=0,
    )
    subheading_style = ParagraphStyle(
        "ApptFormSubheading",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=12,
        alignment=TA_LEFT,
        textColor=brand_muted,
        leading=14,
        spaceAfter=0,
    )
    label_style = ParagraphStyle(
        "ApptFieldLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        textColor=brand_brown,
        leading=12,
    )
    value_style = ParagraphStyle(
        "ApptFieldValue",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        textColor=colors.HexColor("#2A241F"),
        leading=12,
    )

    records = appointments_list if appointments_list else [None]
    for i, appt in enumerate(records):
        date_value = datetime.now().strftime("%B %d, %Y")
        if appt and appt.date:
            date_value = appt.date

        # Header row with logo box + clinic title
        logo_box = Table([["GD"]], colWidths=[0.95 * inch], rowHeights=[0.6 * inch])
        logo_box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 1, brand_brown),
            ("BACKGROUND", (0, 0), (-1, -1), brand_brown),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 11),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ]))
        title_block = Table(
            [[Paragraph("Guerrero Dental", heading_style)], [Paragraph("patient records", subheading_style)]],
            colWidths=[5.7 * inch],
            rowHeights=[0.32 * inch, 0.26 * inch],
        )
        title_block.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        header = Table([[logo_box, title_block]], colWidths=[1.05 * inch, 5.7 * inch], rowHeights=[0.75 * inch])
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 0), (-1, 0), 1, brand_gold),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(header)
        elements.append(Spacer(1, 0.1 * inch))

        # Date line
        date_table = Table(
            [[Paragraph("<b>Date:</b>", label_style), Paragraph(date_value, value_style)]],
            colWidths=[0.7 * inch, 6.0 * inch],
            rowHeights=[0.24 * inch],
        )
        date_table.setStyle(TableStyle([
            ("LINEBELOW", (1, 0), (1, 0), 1, brand_brown),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        elements.append(date_table)
        elements.append(Spacer(1, 0.14 * inch))

        status_val = (appt.status.capitalize() if appt and appt.status else "")
        patient_name = (appt.patient_name if appt and appt.patient_name else "")
        phone_val = ""
        if appt and getattr(appt, "patient", None) and appt.patient.phone:
            phone_val = appt.patient.phone
        assigned_doctor = appt.type if appt and appt.type else ""

        info_rows = [
            [Paragraph("Status:", label_style), Paragraph(status_val, value_style), Paragraph("Date:", label_style), Paragraph(date_value, value_style)],
            [Paragraph("Name:", label_style), Paragraph(patient_name, value_style), "", ""],
            [Paragraph("Phone:", label_style), Paragraph(phone_val, value_style), "", ""],
            [Paragraph("Assigned doctor:", label_style), Paragraph(assigned_doctor, value_style), "", ""],
        ]
        info_table = Table(
            info_rows,
            colWidths=[0.9 * inch, 2.8 * inch, 0.6 * inch, 2.0 * inch],
            rowHeights=[0.32 * inch, 0.32 * inch, 0.32 * inch, 0.38 * inch],
        )
        info_table.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 1.2, brand_brown),
            ("BACKGROUND", (0, 0), (-1, -1), panel_bg),
            ("LINEBELOW", (1, 0), (1, 0), 1, brand_brown),
            ("LINEBELOW", (3, 0), (3, 0), 1, brand_brown),
            ("LINEBELOW", (1, 1), (1, 1), 1, brand_brown),
            ("LINEBELOW", (1, 2), (1, 2), 1, brand_brown),
            ("LINEBELOW", (1, 3), (1, 3), 1, brand_brown),
            ("TEXTCOLOR", (0, 0), (0, -1), brand_brown),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(info_table)
        elements.append(Spacer(1, 0.14 * inch))

        elements.append(Spacer(1, 0.1 * inch))

        if i < len(records) - 1:
            elements.append(PageBreak())

    doc.build(elements, onFirstPage=_draw_doctor_signature_footer, onLaterPages=_draw_doctor_signature_footer)
    buffer.seek(0)
    return buffer


def generate_pdf_inquiries(complaints_list):
    """Generate PDF report for complaint/inquiry records"""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.8*inch, bottomMargin=0.5*inch)
    elements = []
    styles = getSampleStyleSheet()

    # Title
    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor('#7C3D12'),
        spaceAfter=6, alignment=TA_CENTER, fontName='Helvetica-Bold'
    )
    elements.append(Paragraph("Guerrero Dental — Patient Inquiries Backup", title_style))

    # Timestamp
    timestamp_style = ParagraphStyle('Timestamp', parent=styles['Normal'], fontSize=9,
                                     textColor=colors.HexColor('#8A7A6A'), alignment=TA_CENTER)
    elements.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", timestamp_style))
    elements.append(Spacer(1, 0.2*inch))

    # Table data
    data = [['Name', 'Phone', 'Concern', 'Urgency', 'Status', 'Assigned Doctor', 'Created']]

    for c in complaints_list:
        data.append([
            c.name or 'N/A',
            c.phone or 'N/A',
            (c.concern or 'N/A')[:20],
            c.urgency or 'routine',
            c.status or 'new',
            c.assigned_doctor or 'Unassigned',
            c.created_at.strftime('%Y-%m-%d') if c.created_at else 'N/A'
        ])

    # Create table
    table = Table(data, colWidths=[1.2*inch, 1*inch, 1.2*inch, 0.9*inch, 0.9*inch, 1.3*inch, 0.8*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#7C3D12')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#E5DDD3')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAF6F0')]),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ROWHEIGHT', (0, 1), (-1, -1), 18),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 0.3*inch))

    # Footer
    footer_text = f"Total Records: {len(complaints_list)} | For official backup purposes only"
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8,
                                 textColor=colors.HexColor('#8A7A6A'), alignment=TA_CENTER)
    elements.append(Paragraph(footer_text, footer_style))

    doc.build(elements, onFirstPage=_draw_pdf_brand, onLaterPages=_draw_pdf_brand)
    buffer.seek(0)
    return buffer


# ── PDF EXPORT ENDPOINTS ──────────────────────────────────────────────────
@app.route("/api/export/patients/pdf", methods=["GET"])
@require_role("superadmin", "admin")
def export_patients_pdf():
    """Export all patients to PDF backup"""
    patients = Patient.query.order_by(Patient.created_at.desc()).all()
    pdf = generate_pdf_patients(patients)
    filename = f"patients_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.route("/api/export/appointments/pdf", methods=["GET"])
@require_role("superadmin", "admin")
def export_appointments_pdf():
    """Export all appointments to PDF backup"""
    appointments = Appointment.query.order_by(Appointment.date.desc(), Appointment.time).all()
    pdf = generate_pdf_appointments(appointments)
    filename = f"appointments_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.route("/api/export/inquiries/pdf", methods=["GET"])
@require_role("superadmin", "admin")
def export_inquiries_pdf():
    """Export all inquiries/complaints to PDF backup"""
    inquiries = Complaint.query.order_by(Complaint.created_at.desc()).all()
    pdf = generate_pdf_inquiries(inquiries)
    filename = f"inquiries_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.route("/api/export/patient/<int:pid>/pdf", methods=["GET"])
@require_role("superadmin", "admin")
def export_patient_pdf(pid):
    """Export single patient record with appointments"""
    patient = Patient.query.get_or_404(pid)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.8*inch, bottomMargin=0.5*inch)
    elements = []
    styles = getSampleStyleSheet()

    # Title
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16,
                                textColor=colors.HexColor('#7C3D12'), spaceAfter=12, fontName='Helvetica-Bold')
    elements.append(Paragraph(f"Patient Record: {patient.name}", title_style))

    # Patient Info Table
    patient_info = [
        ['Patient ID:', patient.patient_id or 'N/A'],
        ['Name:', patient.name or 'N/A'],
        ['Phone:', patient.phone or 'N/A'],
        ['Email:', patient.email or 'N/A'],
        ['DOB:', patient.dob or 'N/A'],
        ['Address:', patient.address or 'N/A'],
        ['Status:', patient.status or 'N/A'],
        ['Last Visit:', patient.last_visit or 'Never'],
        ['Notes:', patient.notes or 'No notes'],
    ]

    info_table = Table(patient_info, colWidths=[1.5*inch, 4*inch])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F5E6C8')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#E5DDD3')),
        ('ROWHEIGHT', (0, 0), (-1, -1), 18),
    ]))

    elements.append(info_table)
    elements.append(Spacer(1, 0.2*inch))

    # Appointments section
    appointments = patient.appointments
    if appointments:
        appt_title = ParagraphStyle('SectionTitle', parent=styles['Heading2'], fontSize=12,
                                   textColor=colors.HexColor('#7C3D12'), spaceAfter=8, fontName='Helvetica-Bold')
        elements.append(Paragraph("Appointment History", appt_title))

        appt_data = [['Date', 'Time', 'Type', 'Status', 'Notes']]
        for a in appointments:
            appt_data.append([
                a.date or 'N/A',
                a.time or 'N/A',
                a.type or 'N/A',
                a.status or 'N/A',
                (a.notes or '')[:25]
            ])

        appt_table = Table(appt_data, colWidths=[1*inch, 0.8*inch, 1.2*inch, 1*inch, 1.2*inch])
        appt_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#7C3D12')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#E5DDD3')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAF6F0')]),
            ('ROWHEIGHT', (0, 1), (-1, -1), 16),
        ]))
        elements.append(appt_table)

    elements.append(Spacer(1, 0.3*inch))

    # Footer
    footer_text = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | For official purposes only"
    footer_style = ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8,
                                 textColor=colors.HexColor('#8A7A6A'), alignment=TA_CENTER)
    elements.append(Paragraph(footer_text, footer_style))

    doc.build(elements, onFirstPage=_draw_pdf_brand, onLaterPages=_draw_pdf_brand)
    buffer.seek(0)
    filename = f"patient_{patient.patient_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)


# ── SEED ─────────────────────────────────────────────────────────────────
def seed():
    if StaffUser.query.first():
        return

    sa = StaffUser(name="Dr. Bambina Guerrero", email="superadmin@guerrerodental.com", role="superadmin", is_active=True)
    sa.set_password("SuperAdmin2025!")
    ad = StaffUser(name="Admin Reyes",          email="admin@guerrerodental.com",      role="admin",      is_active=True)
    ad.set_password("Admin2025!")
    n1 = StaffUser(name="Nurse Ana Reyes",    email="nurse@guerrerodental.com",  role="nurse", is_active=True)
    n1.set_password("NurseAna2025!")
    n2 = StaffUser(name="Nurse Maria Santos", email="nurse2@guerrerodental.com", role="nurse", is_active=True)
    n2.set_password("NurseMaria2025!")

    db.session.add_all([sa, ad, n1, n2])
    db.session.flush()

    patients = [
        Patient(patient_id="GD1001", name="Juan Dela Cruz",  email="juan@email.com",   phone="0917-111-1111", dob="1985-05-15", address="Quezon City",  status="active",   last_visit="2024-01-15"),
        Patient(patient_id="GD1002", name="Maria Santos",    email="maria@email.com",   phone="0917-222-2222", dob="1990-08-22", address="Pasig",        status="active",   last_visit="2024-01-10"),
        Patient(patient_id="GD1003", name="Pedro Reyes",     email="pedro@email.com",   phone="0917-333-3333", dob="1978-12-03", address="Makati",       status="active",   last_visit="2024-01-05"),
        Patient(patient_id="GD1004", name="Ana Lopez",       email="ana@email.com",     phone="0917-444-4444", dob="1995-03-18", address="Pasig",        status="active",   last_visit="2023-12-20"),
        Patient(patient_id="GD1005", name="Miguel Torres",   email="miguel@email.com",  phone="0917-555-5555", dob="1982-11-30", address="Mandaluyong",  status="inactive", last_visit="2023-11-15"),
    ]
    db.session.add_all(patients)
    db.session.flush()

    today     = datetime.now().strftime("%Y-%m-%d")
    tomorrow  = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    db.session.add_all([
        Appointment(patient_id=patients[0].id, patient_name="Juan Dela Cruz", type="Regular Check-up", date=today,     time="10:00", status="scheduled", notes="Annual check-up"),
        Appointment(patient_id=patients[1].id, patient_name="Maria Santos",   type="Teeth Cleaning",   date=tomorrow,  time="14:00", status="confirmed",  notes="Regular cleaning"),
        Appointment(patient_id=patients[2].id, patient_name="Pedro Reyes",    type="Tooth Extraction", date=tomorrow,  time="11:30", status="scheduled",  notes="Wisdom tooth"),
        Appointment(patient_id=patients[3].id, patient_name="Ana Lopez",      type="Dental Filling",   date=yesterday, time="09:00", status="completed",  notes="Cavity filling"),
    ])
    db.session.add_all([
        Complaint(name="Rosa Bautista",   phone="0917-666-6666", email="rosa@email.com",  concern="Toothache / Severe Pain",     description="Sharp pain on upper right molar for 2 days, worse with cold drinks.", urgency="urgent",  preferred_date=today,    preferred_time="Morning (8am – 12pm)",   status="new"),
        Complaint(name="Carlo Mendoza",   phone="0917-777-7777", email="carlo@email.com", concern="Teeth Whitening / Bleaching", description="Interested in getting my teeth professionally whitened for my wedding.", urgency="routine", preferred_date=tomorrow, preferred_time="Afternoon (12pm – 4pm)", status="new"),
        Complaint(name="Liza Villanueva", phone="0917-888-8888", email="liza@email.com",  concern="General Check-up & Cleaning", description="Haven't been to a dentist in 2 years. Need full cleaning and check-up.", urgency="soon",    preferred_date=tomorrow, preferred_time="Morning (8am – 12pm)",   status="in_review", staff_notes="Called patient, confirmed for Thursday 10am."),
    ])
    db.session.add_all([
        Message(from_name="System",               to_name="All Staff",      subject="New Inquiry Alert",    body="A new urgent inquiry was submitted by Rosa Bautista.", date=today, read=False),
        Message(from_name="Dr. Bambina Guerrero", to_name="Juan Dela Cruz", subject="Appointment Reminder", body="Reminder for your appointment today at 10:00 AM.",     date=today, read=True),
    ])
    db.session.commit()
    print("✅  Demo data seeded successfully.")


# ── AI CHAT (PUBLIC — no auth required) ──────────────────────────────────
import urllib.request, json as _json

@app.route("/api/chat-public", methods=["POST"])
def chat_public():
    data = request.get_json() or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"content": [{"type": "text", "text": "AI chat is not configured. Please call us at 0917-5850-158 or use the inquiry form!"}]})
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": data.get("messages", []),
    }
    if data.get("system"):
        payload["system"] = data["system"]
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return jsonify(_json.loads(r.read()))
    except Exception as e:
        return jsonify({"content": [{"type": "text", "text": "Sorry, I'm having trouble right now. Please call 0917-5850-158!"}]}), 200


# ── AI CHAT (STAFF — JWT required) ───────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
@require_role("superadmin", "admin", "nurse")
def chat_staff():
    data = request.get_json() or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"content": [{"type": "text", "text": "AI chat is not configured. Set ANTHROPIC_API_KEY env var."}]})
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": data.get("messages", []),
    }
    if data.get("system"):
        payload["system"] = data["system"]
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return jsonify(_json.loads(r.read()))
    except Exception as e:
        return jsonify({"content": [{"type": "text", "text": "AI error. Please try again."}]}), 200


# ── SEND APPOINTMENT CONFIRMATION EMAIL TO PATIENT ───────────────────────
@app.route("/api/complaints/<int:cid>/email-patient", methods=["POST"])
@require_role("superadmin", "admin")
def email_patient(cid):
    c = Complaint.query.get_or_404(cid)
    if not c.email:
        return jsonify({"message": "No email address on file for this patient."}), 400
    
    data = request.get_json()
    if not data.get("subject") or not data.get("body"):
        return jsonify({"message": "Subject and body are required."}), 400
    
    try:
        send_email(c.email, data["subject"], data["body"])
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "message": f"Failed to send email: {str(e)}.\nPlease configure MAIL_SERVER, MAIL_PORT, and credentials in environment variables."
        }), 500

    print(f"EMAIL TO: {c.email} ({c.name})")
    print(f"SUBJECT: {data['subject']}")
    print(f"BODY:\n{data['body']}")
    print("-" * 50)
    
    # Create a message record for logging
    user = get_current_user()
    msg = Message(
        from_name=user.name, to_name=c.name,
        subject=data["subject"], body=data["body"],
        date=datetime.now().strftime("%Y-%m-%d"), read=True
    )
    db.session.add(msg)
    db.session.commit()
    
    return jsonify({"message": f"Email sent to {c.name} at {c.email}."}), 200


# ── CONVERT INQUIRY TO PATIENT + APPOINTMENT ──────────────────────────
@app.route("/api/complaints/<int:cid>/convert", methods=["POST"])
@require_role("superadmin", "admin", "nurse")
def convert_inquiry(cid):
    c = Complaint.query.get_or_404(cid)
    if c.status == "closed":
        return jsonify({"message": "Cannot convert a closed inquiry."}), 400
    
    data = request.get_json() or {}
    
    # Create patient
    patient = Patient(
        patient_id=f"GD{1000 + Patient.query.count() + 1}",
        name=c.name, email=c.email, phone=c.phone,
        dob=data.get("dob"), address=data.get("address"),
        status="active"
    )
    db.session.add(patient)
    db.session.flush()  # Get the ID
    
    # Create appointment if requested
    appt = None
    if data.get("schedule_appointment"):
        appt_type = data.get("appointment_type") or c.concern or "General Check-up"
        appt_date = data.get("appointment_date") or c.preferred_date or datetime.now().strftime("%Y-%m-%d")
        appt_time = _normalize_time(data.get("appointment_time", "10:00"))
        
        appt = Appointment(
            patient_id=patient.id, patient_name=patient.name,
            type=appt_type,
            date=appt_date,
            time=appt_time,
            status="scheduled", notes=f"Converted from inquiry: {c.description or ''}"
        )
        db.session.add(appt)
    
    # Update inquiry status
    c.status = "scheduled" if appt else "closed"
    c.staff_notes = (c.staff_notes or "") + f"\n[Converted to patient {patient.patient_id}]"
    
    db.session.commit()
    
    return jsonify({
        "patient": patient.to_dict(),
        "appointment": appt.to_dict() if appt else None,
        "inquiry": c.to_dict(),
        "message": f"Patient record created and {'appointment scheduled.' if appt else 'no appointment scheduled.'}"
    }), 200


def _normalize_time(t):
    """Accept HH:MM or h:MM AM/PM, always store as HH:MM"""
    if not t: return "09:00"
    t = str(t).strip()
    if "AM" in t.upper() or "PM" in t.upper():
        from datetime import datetime as _dt2
        for fmt in ("%I:%M %p", "%I:%M%p"):
            try: return _dt2.strptime(t.upper(), fmt.upper()).strftime("%H:%M")
            except: pass
    return t


# ── DB INIT (runs for both gunicorn and direct python app.py) ────────────
with app.app_context():
    db.create_all()
    seed()

# ── MAIN ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🦷  Guerrero Dental — Full-Stack v3.0")
    print("━" * 60)
    print("   PUBLIC SITE:    http://localhost:5000/")
    print("   Login:          http://localhost:5000/gd-admin/login")
    print("━" * 60 + "\n")
    debug_mode = os.environ.get("FLASK_ENV", "production") == "development"
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
