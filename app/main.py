from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta
import base64
import json
import uuid

import jwt
from pwdlib import PasswordHash
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi

from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    UploadFile,
    File,
    Form,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from openai import OpenAI

from langgraph.graph import StateGraph, START, END
from typing import TypedDict


# ============================================================
# SETTINGS
# ============================================================

class Settings(BaseSettings):
    mongodb_uri: str = ""
    mongodb_database: str = "civic_ai"

    jwt_secret: str = "change-this-secret"

    # Groq
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_transcribe_model: str = "whisper-large-v3-turbo"

    # Demo authority
    authority_email: str = "authority@civic.ai"
    authority_password: str = "Demo@12345"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )


s = Settings()


# ============================================================
# GLOBALS
# ============================================================

client = None
db = None

USE_MONGO = bool(s.mongodb_uri.strip())

LOCAL_USERS = {}
LOCAL_COMPLAINTS = {}

pwd = PasswordHash.recommended()
auth = HTTPBearer(auto_error=False)

BASE_DIR = Path(__file__).resolve().parent.parent

STATIC_DIR = BASE_DIR / "app" / "static"
MEDIA = BASE_DIR / "media"

MEDIA.mkdir(parents=True, exist_ok=True)


# ============================================================
# GROQ CLIENT
# ============================================================

def get_groq_client():
    if not s.groq_api_key:
        return None

    return OpenAI(
        api_key=s.groq_api_key,
        base_url="https://api.groq.com/openai/v1",
    )


# ============================================================
# CIVIC CONFIG
# ============================================================

DEPTS = {
    "road": "PWD",
    "garbage": "Municipality",
    "street_light": "Electrical Department",
    "water": "Water Department",
    "drainage": "Drainage Department",
    "sewage": "Sewerage Department",
    "electricity": "Electrical Department",
    "traffic": "Traffic Police",
    "public_safety": "Police / Public Safety",
    "sanitation": "Sanitation Department",
    "other": "Municipality",
}

CREDITS = {
    "road": 20,
    "garbage": 15,
    "street_light": 15,
    "water": 15,
    "drainage": 15,
    "sewage": 15,
    "electricity": 15,
    "traffic": 20,
    "public_safety": 25,
    "sanitation": 15,
    "other": 10,
}


# ============================================================
# DATABASE LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app):
    global client, db

    if USE_MONGO:

        client = AsyncMongoClient(
            s.mongodb_uri,
            server_api=ServerApi(
                "1",
                strict=True,
                deprecation_errors=True,
            ),
        )

        db = client[s.mongodb_database]

        await db.command("ping")

        await db.users.create_index(
            "email",
            unique=True,
        )

        await db.complaints.create_index(
            "ticket_id",
            unique=True,
        )

        await db.complaints.create_index(
            "user_id"
        )

        if not await db.users.find_one(
            {"email": s.authority_email.lower()}
        ):
            await db.users.insert_one(
                {
                    "_id": uuid.uuid4().hex,
                    "name": "Civic Authority",
                    "email": s.authority_email.lower(),
                    "password": pwd.hash(
                        s.authority_password
                    ),
                    "role": "authority",
                    "credits": 0,
                }
            )

        print("[Civic AI] Connected to MongoDB Atlas")

    else:

        LOCAL_USERS[
            s.authority_email.lower()
        ] = {
            "_id": "authority-demo",
            "name": "Civic Authority",
            "email": s.authority_email.lower(),
            "password": pwd.hash(
                s.authority_password
            ),
            "role": "authority",
            "credits": 0,
        }

        print(
            "[Civic AI] MongoDB URI not configured "
            "-> LOCAL DEMO MODE"
        )

    if s.groq_api_key:
        print(
            f"[Civic AI] Groq enabled -> {s.groq_model}"
        )
    else:
        print(
            "[Civic AI] GROQ_API_KEY not configured "
            "-> LOCAL AI FALLBACK"
        )

    yield

    if client:
        await client.close()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Civic AI",
    lifespan=lifespan,
)

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)

app.mount(
    "/media",
    StaticFiles(directory=str(MEDIA)),
    name="media",
)


@app.get("/", response_class=HTMLResponse)
async def home():
    return (
        STATIC_DIR / "index.html"
    ).read_text(encoding="utf-8")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "database": "mongodb" if USE_MONGO else "local",
        "ai": "groq" if s.groq_api_key else "fallback",
    }


# ============================================================
# USER HELPERS
# ============================================================

async def find_user_by_email(email):
    if USE_MONGO:
        return await db.users.find_one(
            {"email": email}
        )

    return LOCAL_USERS.get(email)


async def find_user_by_id(uid):

    if USE_MONGO:
        return await db.users.find_one(
            {"_id": uid}
        )

    return next(
        (
            u
            for u in LOCAL_USERS.values()
            if u["_id"] == uid
        ),
        None,
    )


async def insert_user(doc):

    if USE_MONGO:
        await db.users.insert_one(doc)

    else:
        LOCAL_USERS[doc["email"]] = doc


async def add_credits(uid, amount):

    if USE_MONGO:

        await db.users.update_one(
            {"_id": uid},
            {
                "$inc": {
                    "credits": amount
                }
            },
        )

    else:

        u = await find_user_by_id(uid)

        if u:
            u["credits"] = (
                u.get("credits", 0)
                + amount
            )


# ============================================================
# COMPLAINT HELPERS
# ============================================================

async def insert_complaint(doc):

    if USE_MONGO:
        await db.complaints.insert_one(doc)

    else:
        LOCAL_COMPLAINTS[
            doc["ticket_id"]
        ] = doc


async def get_complaint(tid):

    if USE_MONGO:
        return await db.complaints.find_one(
            {"ticket_id": tid}
        )

    return LOCAL_COMPLAINTS.get(tid)


async def update_complaint(
    tid,
    changes,
):

    if USE_MONGO:

        await db.complaints.update_one(
            {"ticket_id": tid},
            {
                "$set": changes
            },
        )

    elif tid in LOCAL_COMPLAINTS:

        LOCAL_COMPLAINTS[tid].update(
            changes
        )


async def list_complaints(
    user_id=None
):

    if USE_MONGO:

        q = (
            {"user_id": user_id}
            if user_id
            else {}
        )

        return await (
            db.complaints
            .find(q)
            .sort("created_at", -1)
            .to_list(200)
        )

    docs = list(
        LOCAL_COMPLAINTS.values()
    )

    if user_id:

        docs = [
            d
            for d in docs
            if d.get("user_id")
            == user_id
        ]

    return sorted(
        docs,
        key=lambda d: d.get(
            "created_at",
            datetime.min.replace(
                tzinfo=timezone.utc
            ),
        ),
        reverse=True,
    )[:200]


async def recent_active(issue_type):

    if USE_MONGO:

        return await (
            db.complaints
            .find(
                {
                    "issue_type": issue_type,
                    "status": {
                        "$ne": "RESOLVED"
                    },
                },
                {
                    "ticket_id": 1,
                    "text": 1,
                },
            )
            .limit(80)
            .to_list(80)
        )

    return [
        {
            "ticket_id": d["ticket_id"],
            "text": d.get("text", ""),
        }
        for d in LOCAL_COMPLAINTS.values()
        if (
            d.get("issue_type")
            == issue_type
            and d.get("status")
            != "RESOLVED"
        )
    ][:80]


async def complaint_count():

    if USE_MONGO:
        return await db.complaints.count_documents({})

    return len(LOCAL_COMPLAINTS)


# ============================================================
# AUTH
# ============================================================

def token(uid, role):

    return jwt.encode(
        {
            "sub": uid,
            "role": role,
            "exp": (
                datetime.now(timezone.utc)
                + timedelta(days=1)
            ),
        },
        s.jwt_secret,
        algorithm="HS256",
    )


async def user(
    creds: HTTPAuthorizationCredentials =
    Depends(auth),
):

    if not creds:
        raise HTTPException(
            401,
            "Login required",
        )

    try:

        payload = jwt.decode(
            creds.credentials,
            s.jwt_secret,
            algorithms=["HS256"],
        )

    except Exception:

        raise HTTPException(
            401,
            "Invalid token",
        )

    u = await find_user_by_id(
        payload["sub"]
    )

    if not u:

        raise HTTPException(
            401,
            "User not found",
        )

    return u


async def authority(
    u=Depends(user),
):

    if u["role"] != "authority":

        raise HTTPException(
            403,
            "Authority access required",
        )

    return u


# ============================================================
# AUTH MODELS
# ============================================================

class Signup(BaseModel):
    name: str
    email: EmailStr
    password: str


class Login(BaseModel):
    email: EmailStr
    password: str


# ============================================================
# AUTH ROUTES
# ============================================================

@app.post("/api/signup")
async def signup(x: Signup):

    email = str(x.email).lower()

    if len(x.password) < 6:

        raise HTTPException(
            400,
            "Password must be 6+ characters",
        )

    if await find_user_by_email(email):

        raise HTTPException(
            409,
            "Email already registered",
        )

    uid = uuid.uuid4().hex

    await insert_user(
        {
            "_id": uid,
            "name": x.name.strip(),
            "email": email,
            "password": pwd.hash(
                x.password
            ),
            "role": "citizen",
            "credits": 0,
        }
    )

    return {
        "token": token(
            uid,
            "citizen",
        )
    }


@app.post("/api/login")
async def login(x: Login):

    u = await find_user_by_email(
        str(x.email).lower()
    )

    if (
        not u
        or not pwd.verify(
            x.password,
            u["password"],
        )
    ):

        raise HTTPException(
            401,
            "Invalid credentials",
        )

    return {
        "token": token(
            u["_id"],
            u["role"],
        )
    }


@app.get("/api/me")
async def me(
    u=Depends(user),
):

    return {
        "id": u["_id"],
        "name": u["name"],
        "email": u["email"],
        "role": u["role"],
        "credits": u.get(
            "credits",
            0,
        ),
    }


# ============================================================
# CLEAN RESPONSE
# ============================================================

def clean(d):

    if not d:
        return d

    d = {
        k: v
        for k, v in d.items()
        if k != "_id"
    }

    for k in (
        "created_at",
        "updated_at",
    ):

        if hasattr(
            d.get(k),
            "isoformat",
        ):

            d[k] = d[k].isoformat()

    if d.get("image_path"):

        d["image_url"] = (
            "/media/"
            + Path(
                d["image_path"]
            ).name
        )

    if d.get(
        "completion_image_path"
    ):

        d["completion_image_url"] = (
            "/media/"
            + Path(
                d[
                    "completion_image_path"
                ]
            ).name
        )

    return d


# ============================================================
# LOCAL FALLBACK AI
# ============================================================

def fallback_ai(text):

    l = text.lower()

    if any(
        w in l
        for w in [
            "pothole",
            "road",
            "street",
            "crack",
        ]
    ):
        issue = "road"

    elif any(
        w in l
        for w in [
            "garbage",
            "trash",
            "waste",
        ]
    ):
        issue = "garbage"

    elif "water" in l:
        issue = "water"

    elif any(
        w in l
        for w in [
            "light",
            "streetlight",
            "street light",
        ]
    ):
        issue = "street_light"

    elif any(
        w in l
        for w in [
            "drain",
            "drainage",
        ]
    ):
        issue = "drainage"

    elif any(
        w in l
        for w in [
            "sewage",
            "sewer",
        ]
    ):
        issue = "sewage"

    elif "electric" in l:
        issue = "electricity"

    elif "traffic" in l:
        issue = "traffic"

    elif any(
        w in l
        for w in [
            "danger",
            "crime",
            "unsafe",
        ]
    ):
        issue = "public_safety"

    else:
        issue = "other"

    if any(
        w in l
        for w in [
            "danger",
            "accident",
            "unsafe",
            "emergency",
        ]
    ):
        severity = "high"
        score = 0.88

    elif any(
        w in l
        for w in [
            "urgent",
            "blocked",
            "major",
        ]
    ):
        severity = "moderate"
        score = 0.68

    else:
        severity = "low"
        score = 0.45

    return {
        "issue_type": issue,
        "description": text[:500],
        "severity": severity,
        "severity_score": score,
        "language": "English",
    }


# ============================================================
# GROQ AI ANALYSIS
# ============================================================

def ai_analyze(
    text,
    image,
):

    # No Groq key -> fallback
    if not s.groq_api_key:

        return fallback_ai(text)

    client = get_groq_client()

    content = [
        {
            "type": "input_text",
            "text": f"""
You are Civic AI, an intelligent
municipal complaint analysis system.

Analyze this civic complaint.

Return ONLY valid JSON.

Required fields:

language
issue_type
description
severity
severity_score

Allowed issue_type values:

road
garbage
street_light
water
drainage
sewage
electricity
traffic
public_safety
sanitation
other

Allowed severity values:

critical
high
moderate
low

Complaint:

{text}
""",
        }
    ]

    # Add image when provided
    if image and Path(image).exists():

        image_bytes = Path(
            image
        ).read_bytes()

        encoded = base64.b64encode(
            image_bytes
        ).decode()

        suffix = (
            Path(image)
            .suffix
            .lower()
        )

        if suffix == ".png":
            mime = "image/png"

        elif suffix == ".webp":
            mime = "image/webp"

        else:
            mime = "image/jpeg"

        content.append(
            {
                "type": "input_image",
                "detail": "auto",
                "image_url":
                    f"data:{mime};base64,{encoded}",
            }
        )

    response = client.responses.create(
        model=s.groq_model,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )

    raw = (
        response.output_text
        .strip()
        .replace(
            "```json",
            "",
        )
        .replace(
            "```",
            "",
        )
    )

    start = raw.find("{")
    end = raw.rfind("}")

    if start == -1 or end == -1:

        return fallback_ai(text)

    try:

        result = json.loads(
            raw[start:end + 1]
        )

        return result

    except json.JSONDecodeError:

        return fallback_ai(text)


# ============================================================
# LANGGRAPH STATE
# ============================================================

class State(TypedDict, total=False):

    text: str
    image_path: str | None
    user_id: str

    issue_type: str
    description: str
    severity: str
    severity_score: float
    language: str

    department: str
    credits: int

    duplicate: bool
    duplicate_of: str | None

    ticket_id: str


# ============================================================
# LANGGRAPH NODES
# ============================================================

async def n_ai(x):

    result = ai_analyze(
        x.get("text", ""),
        x.get("image_path"),
    )

    return result


async def n_route(x):

    issue = x.get(
        "issue_type",
        "other",
    )

    return {
        "department": DEPTS.get(
            issue,
            "Municipality",
        ),
        "credits": CREDITS.get(
            issue,
            10,
        ),
    }


async def n_dup(x):

    docs = await recent_active(
        x["issue_type"]
    )

    words = set(
        x.get(
            "text",
            "",
        )
        .lower()
        .split()
    )

    best = None

    for d in docs:

        other_words = set(
            (
                d.get(
                    "text",
                    "",
                )
                or ""
            )
            .lower()
            .split()
        )

        similarity = (
            len(
                words
                & other_words
            )
            / max(
                1,
                len(
                    words
                    | other_words
                ),
            )
        )

        if similarity >= 0.65:

            best = d
            break

    return {
        "duplicate": bool(best),
        "duplicate_of": (
            best.get("ticket_id")
            if best
            else None
        ),
        "credits": (
            0
            if best
            else x["credits"]
        ),
    }


async def n_save(x):

    now = datetime.now(
        timezone.utc
    )

    count = (
        await complaint_count()
        + 1
    )

    ticket = (
        f"CIV-{now.strftime('%Y%m%d')}-"
        f"{str(count).zfill(4)}"
    )

    doc = {
        **x,
        "ticket_id": ticket,
        "status": "ASSIGNED",
        "created_at": now,
        "updated_at": now,
    }

    await insert_complaint(doc)

    await add_credits(
        x["user_id"],
        x["credits"],
    )

    return {
        "ticket_id": ticket
    }


# ============================================================
# BUILD LANGGRAPH
# ============================================================

graph = StateGraph(State)

graph.add_node(
    "ai_analysis",
    n_ai,
)

graph.add_node(
    "routing",
    n_route,
)

graph.add_node(
    "duplicate_check",
    n_dup,
)

graph.add_node(
    "save",
    n_save,
)

graph.add_edge(
    START,
    "ai_analysis",
)

graph.add_edge(
    "ai_analysis",
    "routing",
)

graph.add_edge(
    "routing",
    "duplicate_check",
)

graph.add_edge(
    "duplicate_check",
    "save",
)

graph.add_edge(
    "save",
    END,
)

GRAPH = graph.compile()


# ============================================================
# RESOLUTION VERIFICATION
# ============================================================

def verify(
    text,
    message,
):

    # No Groq -> simple fallback
    if not s.groq_api_key:

        return {
            "verified": bool(
                message.strip()
            ),
            "score": (
                0.88
                if message.strip()
                else 0.20
            ),
            "reason": (
                "Authority evidence received."
            ),
        }

    client = get_groq_client()

    response = client.responses.create(
        model=s.groq_model,
        input=f"""
You are Civic AI's resolution
verification system.

Determine whether the authority
response indicates that the civic
complaint has actually been resolved.

Return ONLY valid JSON:

{{
    "verified": true or false,
    "score": 0-1,
    "reason": "short explanation"
}}

Original complaint:

{text}

Authority response:

{message}
""",
    )

    raw = (
        response.output_text
        .strip()
        .replace(
            "```json",
            "",
        )
        .replace(
            "```",
            "",
        )
    )

    start = raw.find("{")
    end = raw.rfind("}")

    if start == -1 or end == -1:

        return {
            "verified": False,
            "score": 0.2,
            "reason": (
                "AI verification returned "
                "an invalid response."
            ),
        }

    try:

        return json.loads(
            raw[start:end + 1]
        )

    except json.JSONDecodeError:

        return {
            "verified": False,
            "score": 0.2,
            "reason": (
                "AI verification response "
                "could not be parsed."
            ),
        }


# ============================================================
# CREATE COMPLAINT
# ============================================================

@app.post("/api/complaints")
async def create(
    text: str = Form(""),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    image: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
    u=Depends(user),
):

    image_path = None

    # --------------------------------------------------------
    # SAVE IMAGE
    # --------------------------------------------------------

    if image and image.filename:

        extension = (
            Path(
                image.filename
            ).suffix
            or ".jpg"
        )

        image_path = str(
            MEDIA
            / f"{uuid.uuid4().hex}"
            f"{extension}"
        )

        with open(
            image_path,
            "wb",
        ) as f:

            f.write(
                await image.read()
            )

    # --------------------------------------------------------
    # AUDIO TRANSCRIPTION
    # --------------------------------------------------------

    if (
        not text.strip()
        and audio
        and audio.filename
    ):

        extension = (
            Path(
                audio.filename
            ).suffix
            or ".webm"
        )

        audio_path = str(
            MEDIA
            / f"{uuid.uuid4().hex}"
            f"{extension}"
        )

        with open(
            audio_path,
            "wb",
        ) as f:

            f.write(
                await audio.read()
            )

        if s.groq_api_key:

            client = get_groq_client()

            try:

                with open(
                    audio_path,
                    "rb",
                ) as audio_file:

                    transcription = (
                        client.audio.transcriptions.create(
                            model=(
                                s.groq_transcribe_model
                            ),
                            file=audio_file,
                        )
                    )

                text = transcription.text

            except Exception as e:

                print(
                    "[Civic AI] "
                    f"Transcription failed: {e}"
                )

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if (
        not text.strip()
        and not image_path
    ):

        raise HTTPException(
            400,
            "Add text, voice, or photo",
        )

    # --------------------------------------------------------
    # LANGGRAPH
    # --------------------------------------------------------

    result = await GRAPH.ainvoke(
        {
            "user_id": u["_id"],
            "text": text.strip(),
            "image_path": image_path,
            "latitude": latitude,
            "longitude": longitude,
        }
    )

    complaint = await get_complaint(
        result["ticket_id"]
    )

    return clean(complaint)


# ============================================================
# CITIZEN COMPLAINTS
# ============================================================

@app.get("/api/complaints")
async def my_complaints(
    u=Depends(user),
):

    complaints = await list_complaints(
        u["_id"]
    )

    return [
        clean(c)
        for c in complaints
    ]


# ============================================================
# AUTHORITY
# ============================================================

@app.get("/api/authority/tickets")
async def tickets(
    u=Depends(authority),
):

    complaints = await list_complaints()

    return [
        clean(c)
        for c in complaints
    ]


@app.post(
    "/api/authority/tickets/{tid}/start"
)
async def start(
    tid: str,
    u=Depends(authority),
):

    complaint = await get_complaint(
        tid
    )

    if not complaint:

        raise HTTPException(
            404,
            "Ticket not found",
        )

    await update_complaint(
        tid,
        {
            "status": "IN_PROGRESS",
            "updated_at": datetime.now(
                timezone.utc
            ),
        },
    )

    return {
        "ok": True
    }


# ============================================================
# AUTHORITY COMPLETION
# ============================================================

@app.post(
    "/api/authority/tickets/{tid}/complete"
)
async def complete(
    tid: str,
    message: str = Form(...),
    image: UploadFile | None = File(None),
    u=Depends(authority),
):

    complaint = await get_complaint(
        tid
    )

    if not complaint:

        raise HTTPException(
            404,
            "Ticket not found",
        )

    completion_image_path = None

    # --------------------------------------------------------
    # SAVE COMPLETION IMAGE
    # --------------------------------------------------------

    if image and image.filename:

        extension = (
            Path(
                image.filename
            ).suffix
            or ".jpg"
        )

        completion_image_path = str(
            MEDIA
            / (
                "completion-"
                f"{uuid.uuid4().hex}"
                f"{extension}"
            )
        )

        with open(
            completion_image_path,
            "wb",
        ) as f:

            f.write(
                await image.read()
            )

    # --------------------------------------------------------
    # AI VERIFICATION
    # --------------------------------------------------------

    verification = verify(
        complaint.get(
            "text",
            "",
        ),
        message,
    )

    status = (
        "RESOLVED"
        if verification.get(
            "verified"
        )
        else "HUMAN_REVIEW"
    )

    await update_complaint(
        tid,
        {
            "authority_message": message,
            "completion_image_path":
                completion_image_path,
            "resolution_verified":
                bool(
                    verification.get(
                        "verified"
                    )
                ),
            "resolution_score":
                float(
                    verification.get(
                        "score",
                        0,
                    )
                ),
            "resolution_reason":
                verification.get(
                    "reason",
                    "",
                ),
            "status": status,
            "updated_at": datetime.now(
                timezone.utc
            ),
        },
    )

    return {
        "status": status,
        "score": verification.get(
            "score",
            0,
        ),
        "reason": verification.get(
            "reason",
            "",
        ),
    }