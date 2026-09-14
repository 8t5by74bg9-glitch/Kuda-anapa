
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine, Column, String, Integer, Boolean, Text, DateTime, select, func
from sqlalchemy.orm import declarative_base, sessionmaker
from pydantic import BaseModel
from typing import Optional, Any
from pathlib import Path
import os, json, datetime, uuid, hmac, secrets, re
import httpx
import qrcode
from io import BytesIO

BASE = Path(__file__).resolve().parent
SEED_PATH = BASE / "seed.json"

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(BASE/'kuda.db').as_posix()}")
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE-ME-BEFORE-HOSTING")
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "kuda-admin-2026")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1","true","yes","on"}

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

class Content(Base):
    __tablename__ = "content"
    id = Column(String, primary_key=True)
    type = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    active = Column(Boolean, default=True, index=True)
    featured = Column(Boolean, default=False)
    partner = Column(Boolean, default=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

class Visit(Base):
    __tablename__ = "visits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    hotel = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)

class CardView(Base):
    __tablename__ = "card_views"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content_id = Column(String, nullable=False, index=True)
    hotel = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)

class SavedRoute(Base):
    __tablename__ = "saved_routes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String, unique=True, nullable=False, index=True)
    hotel = Column(String, nullable=False, index=True)
    item_ids = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)

class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content_id = Column(String, nullable=True)
    hotel = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    status = Column(String, nullable=False, default="new", index=True)
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False)

def init_db():
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        if not s.scalar(select(func.count(Content.id))) and SEED_PATH.exists():
            for item in json.loads(SEED_PATH.read_text(encoding="utf-8")):
                x = dict(item)
                iid = x.pop("id")
                s.add(Content(
                    id=iid,
                    type=x.get("type","activity"),
                    title=x.get("title",""),
                    payload=json.dumps(x, ensure_ascii=False),
                    active=bool(x.get("active", True)),
                    featured=bool(x.get("featured", False)),
                    partner=bool(x.get("partner", False)),
                    created_at=utcnow(),
                    updated_at=utcnow()
                ))
            s.commit()

def content_to_dict(x: Content):
    p = json.loads(x.payload)
    p.update({
        "id": x.id, "type": x.type, "title": x.title,
        "active": bool(x.active), "featured": bool(x.featured), "partner": bool(x.partner)
    })
    return p

def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401, detail="admin auth required")

def safe_hotel(v: str):
    v = (v or "direct").strip().lower()
    return re.sub(r"[^a-z0-9_-]", "", v)[:64] or "direct"

async def telegram_notify(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text}
            )
    except Exception:
        pass

app = FastAPI(title="КУДА? Анапа", version="12.0")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=str(BASE/"static")), name="static")
templates = Jinja2Templates(directory=str(BASE/"templates"))

@app.on_event("startup")
def startup():
    init_db()

class ContentIn(BaseModel):
    data: dict[str, Any]

class LeadIn(BaseModel):
    content_id: Optional[str] = None
    hotel: str = "direct"
    name: str
    phone: str

class RouteIn(BaseModel):
    hotel: str = "direct"
    item_ids: list[str]

class TrackIn(BaseModel):
    hotel: str = "direct"
    content_id: Optional[str] = None

class LeadUpdate(BaseModel):
    status: Optional[str] = None
    note: Optional[str] = None

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/place/{iid}", response_class=HTMLResponse)
def place_page(request: Request, iid: str):
    with SessionLocal() as s:
        x = s.get(Content, iid)
        if not x or not x.active:
            raise HTTPException(404)
        item = content_to_dict(x)
    return templates.TemplateResponse("place.html", {"request": request, "item": item})

@app.get("/r/{token}", response_class=HTMLResponse)
def public_route(request: Request, token: str):
    with SessionLocal() as s:
        r = s.scalar(select(SavedRoute).where(SavedRoute.token == token))
        if not r:
            raise HTTPException(404)
        ids = json.loads(r.item_ids)
        items = []
        for iid in ids:
            x = s.get(Content, iid)
            if x and x.active:
                items.append(content_to_dict(x))
    return templates.TemplateResponse("route.html", {
        "request": request, "items": items, "hotel": r.hotel, "token": token
    })

@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/admin/login", response_class=HTMLResponse)
def login(request: Request, login: str=Form(...), password: str=Form(...)):
    if hmac.compare_digest(login, ADMIN_LOGIN) and hmac.compare_digest(password, ADMIN_PASSWORD):
        request.session["admin"] = True
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error":"Неверный логин или пароль"}, status_code=401)

@app.post("/admin/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/api/content")
def api_content(include_inactive: bool=False):
    with SessionLocal() as s:
        q = select(Content)
        if not include_inactive:
            q = q.where(Content.active == True)
        rows = s.scalars(q.order_by(Content.featured.desc(), Content.title.asc())).all()
        return [content_to_dict(x) for x in rows]

@app.get("/api/content/{iid}")
def api_content_one(iid: str):
    with SessionLocal() as s:
        x=s.get(Content,iid)
        if not x: raise HTTPException(404)
        return content_to_dict(x)

@app.post("/api/content")
def api_content_create(request: Request, body: ContentIn):
    require_admin(request)
    item=dict(body.data)
    if not item.get("title"):
        raise HTTPException(400,"title required")
    iid=item.get("id") or f"item-{uuid.uuid4().hex[:10]}"
    item["id"]=iid
    with SessionLocal() as s:
        if s.get(Content,iid):
            raise HTTPException(409,"duplicate")
        s.add(Content(
            id=iid, type=item.get("type","activity"), title=item["title"],
            payload=json.dumps({k:v for k,v in item.items() if k!="id"}, ensure_ascii=False),
            active=bool(item.get("active",True)),
            featured=bool(item.get("featured",False)),
            partner=bool(item.get("partner",False)),
            created_at=utcnow(), updated_at=utcnow()
        ))
        s.commit()
    return {"ok":True,"id":iid}

@app.put("/api/content/{iid}")
def api_content_update(request: Request, iid: str, body: ContentIn):
    require_admin(request)
    item=dict(body.data)
    if not item.get("title"):
        raise HTTPException(400,"title required")
    with SessionLocal() as s:
        x=s.get(Content,iid)
        if not x: raise HTTPException(404)
        x.type=item.get("type","activity")
        x.title=item["title"]
        x.payload=json.dumps({k:v for k,v in item.items() if k!="id"}, ensure_ascii=False)
        x.active=bool(item.get("active",True))
        x.featured=bool(item.get("featured",False))
        x.partner=bool(item.get("partner",False))
        x.updated_at=utcnow()
        s.commit()
    return {"ok":True}

@app.delete("/api/content/{iid}")
def api_content_delete(request: Request, iid: str):
    require_admin(request)
    with SessionLocal() as s:
        x=s.get(Content,iid)
        if x:
            s.delete(x); s.commit()
    return {"ok":True}

@app.post("/api/track/visit")
def api_visit(body: TrackIn):
    with SessionLocal() as s:
        s.add(Visit(hotel=safe_hotel(body.hotel), created_at=utcnow()))
        s.commit()
    return {"ok":True}

@app.post("/api/track/view")
def api_view(body: TrackIn):
    if body.content_id:
        with SessionLocal() as s:
            s.add(CardView(content_id=body.content_id, hotel=safe_hotel(body.hotel), created_at=utcnow()))
            s.commit()
    return {"ok":True}

@app.post("/api/routes")
def api_route_create(body: RouteIn, request: Request):
    token = secrets.token_urlsafe(7)
    hotel = safe_hotel(body.hotel)
    with SessionLocal() as s:
        s.add(SavedRoute(token=token, hotel=hotel, item_ids=json.dumps(body.item_ids), created_at=utcnow()))
        s.commit()
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return {"ok":True,"token":token,"url":f"{base}/r/{token}"}

@app.post("/api/leads")
async def api_lead_create(body: LeadIn):
    hotel=safe_hotel(body.hotel)
    with SessionLocal() as s:
        lead=Lead(
            content_id=body.content_id, hotel=hotel,
            name=body.name.strip()[:120], phone=body.phone.strip()[:80],
            status="new", note="", created_at=utcnow()
        )
        s.add(lead); s.commit(); lid=lead.id
    await telegram_notify(
        f"Новая заявка «КУДА?» #{lid}\n"
        f"Отель: {hotel}\nИмя: {body.name}\nТелефон: {body.phone}\n"
        f"Объект: {body.content_id or '—'}"
    )
    return {"ok":True,"id":lid}

@app.get("/api/admin/dashboard")
def api_dashboard(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        stats={
            "content":s.scalar(select(func.count(Content.id))),
            "active":s.scalar(select(func.count(Content.id)).where(Content.active==True)),
            "visits":s.scalar(select(func.count(Visit.id))),
            "views":s.scalar(select(func.count(CardView.id))),
            "routes":s.scalar(select(func.count(SavedRoute.id))),
            "leads":s.scalar(select(func.count(Lead.id))),
        }
        hotels=[{"hotel":h,"visits":n} for h,n in s.execute(
            select(Visit.hotel,func.count(Visit.id)).group_by(Visit.hotel).order_by(func.count(Visit.id).desc())
        ).all()]
        top=[{"content_id":cid,"views":n} for cid,n in s.execute(
            select(CardView.content_id,func.count(CardView.id)).group_by(CardView.content_id).order_by(func.count(CardView.id).desc()).limit(10)
        ).all()]
        leads=[{
            "id":x.id,"content_id":x.content_id,"hotel":x.hotel,"name":x.name,"phone":x.phone,
            "status":x.status,"note":x.note,"created_at":x.created_at.isoformat()
        } for x in s.scalars(select(Lead).order_by(Lead.id.desc()).limit(200)).all()]
    return {"stats":stats,"hotels":hotels,"top":top,"leads":leads}

@app.patch("/api/leads/{lead_id}")
def api_lead_update(request: Request, lead_id: int, body: LeadUpdate):
    require_admin(request)
    with SessionLocal() as s:
        x=s.get(Lead,lead_id)
        if not x: raise HTTPException(404)
        if body.status is not None:
            if body.status not in {"new","contacted","booked","cancelled"}:
                raise HTTPException(400,"bad status")
            x.status=body.status
        if body.note is not None:
            x.note=body.note[:1000]
        s.commit()
    return {"ok":True}


@app.get("/api/admin/qr")
def api_admin_qr(request: Request, hotel: str = "direct"):
    require_admin(request)
    slug = safe_hotel(hotel)
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    target = f"{base}/?hotel={slug}"
    img = qrcode.make(target)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="kuda-{slug}-qr.png"', "X-Kuda-Target": target}
    )

@app.get("/health")
def health():
    return {
        "ok":True,
        "version":"12.0",
        "database":"postgresql" if DATABASE_URL.startswith("postgres") else "sqlite"
    }
