from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta
import base64, json, uuid, shutil
import jwt
from pwdlib import PasswordHash
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from openai import OpenAI
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

class Settings(BaseSettings):
    mongodb_uri:str; mongodb_database:str="civic_ai"; jwt_secret:str="change-me"; openai_api_key:str=""; openai_model:str="gpt-5.6-luna"; openai_transcribe_model:str="gpt-4o-mini-transcribe"; authority_email:str="authority@civic.ai"; authority_password:str="Demo@12345"
    model_config=SettingsConfigDict(env_file=".env",extra="ignore",case_sensitive=False)
s=Settings(); client=None; db=None
pwd=PasswordHash.recommended(); auth=HTTPBearer(auto_error=False); MEDIA=Path("/app/media"); MEDIA.mkdir(exist_ok=True)
DEPTS={"road":"PWD","garbage":"Municipality","street_light":"Electrical Department","water":"Water Department","drainage":"Drainage Department","sewage":"Sewerage Department","electricity":"Electrical Department","traffic":"Traffic Police","public_safety":"Police / Public Safety","sanitation":"Sanitation Department","other":"Municipality"}
CREDITS={"road":20,"garbage":15,"street_light":15,"water":15,"drainage":15,"sewage":15,"electricity":15,"traffic":20,"public_safety":25,"sanitation":15,"other":10}

@asynccontextmanager
async def lifespan(app):
    global client,db
    client=AsyncMongoClient(s.mongodb_uri,server_api=ServerApi("1",strict=True,deprecation_errors=True)); db=client[s.mongodb_database]; await db.command("ping")
    await db.users.create_index("email",unique=True); await db.complaints.create_index("ticket_id",unique=True); await db.complaints.create_index("user_id")
    if not await db.users.find_one({"email":s.authority_email.lower()}):
        await db.users.insert_one({"_id":uuid.uuid4().hex,"name":"Civic Authority","email":s.authority_email.lower(),"password":pwd.hash(s.authority_password),"role":"authority","credits":0})
    yield
    await client.close()
app=FastAPI(title="Civic AI",lifespan=lifespan); app.mount("/static",StaticFiles(directory="/app/app/static"),name="static"); app.mount("/media",StaticFiles(directory="/app/media"),name="media")
@app.get("/",response_class=HTMLResponse)
async def home(): return Path("/app/app/static/index.html").read_text()
@app.get("/health")
async def health(): return {"ok":True}

def token(uid,role): return jwt.encode({"sub":uid,"role":role,"exp":datetime.now(timezone.utc)+timedelta(days=1)},s.jwt_secret,algorithm="HS256")
async def user(creds:HTTPAuthorizationCredentials=Depends(auth)):
    if not creds: raise HTTPException(401,"Login required")
    try: p=jwt.decode(creds.credentials,s.jwt_secret,algorithms=["HS256"])
    except: raise HTTPException(401,"Invalid token")
    u=await db.users.find_one({"_id":p["sub"]})
    if not u: raise HTTPException(401,"User not found")
    return u
async def authority(u=Depends(user)):
    if u["role"]!="authority": raise HTTPException(403,"Authority access required")
    return u
class Signup(BaseModel): name:str; email:EmailStr; password:str
class Login(BaseModel): email:EmailStr; password:str
@app.post("/api/signup")
async def signup(x:Signup):
    e=str(x.email).lower()
    if len(x.password)<6: raise HTTPException(400,"Password must be 6+ characters")
    if await db.users.find_one({"email":e}): raise HTTPException(409,"Email already registered")
    uid=uuid.uuid4().hex; await db.users.insert_one({"_id":uid,"name":x.name.strip(),"email":e,"password":pwd.hash(x.password),"role":"citizen","credits":0})
    return {"token":token(uid,"citizen")}
@app.post("/api/login")
async def login(x:Login):
    u=await db.users.find_one({"email":str(x.email).lower()})
    if not u or not pwd.verify(x.password,u["password"]): raise HTTPException(401,"Invalid credentials")
    return {"token":token(u["_id"],u["role"])}
@app.get("/api/me")
async def me(u=Depends(user)): return {"id":u["_id"],"name":u["name"],"email":u["email"],"role":u["role"],"credits":u.get("credits",0)}

def clean(d):
    d={k:v for k,v in d.items() if k!="_id"};
    for k in ("created_at","updated_at"):
        if hasattr(d.get(k),"isoformat"): d[k]=d[k].isoformat()
    if d.get("image_path"): d["image_url"]="/media/"+Path(d["image_path"]).name
    if d.get("completion_image_path"): d["completion_image_url"]="/media/"+Path(d["completion_image_path"]).name
    return d

def ai_analyze(text,image):
    if not s.openai_api_key:
        l=text.lower(); issue="road" if any(w in l for w in ["pothole","road","street"]) else "garbage" if any(w in l for w in ["garbage","trash","waste"]) else "water" if "water" in l else "street_light" if "light" in l else "other"; sev="high" if any(w in l for w in ["danger","accident","unsafe"]) else "moderate"; return {"issue_type":issue,"description":text[:500],"severity":sev,"severity_score":.82 if sev=="high" else .62,"language":"English"}
    c=OpenAI(api_key=s.openai_api_key); content=[{"type":"input_text","text":f'''Analyze this civic complaint. Return ONLY JSON with language, issue_type, description, severity, severity_score. issue_type: road, garbage, street_light, water, drainage, sewage, electricity, traffic, public_safety, sanitation, other. severity: critical, high, moderate, low. Complaint: {text}'''}]
    if image and Path(image).exists():
        b=base64.b64encode(Path(image).read_bytes()).decode(); mime="image/png" if Path(image).suffix.lower()==".png" else "image/jpeg"; content.append({"type":"input_image","image_url":f"data:{mime};base64,{b}"})
    r=c.responses.create(model=s.openai_model,input=[{"role":"user","content":content}]); raw=r.output_text.strip().replace("```json","").replace("```",""); return json.loads(raw[raw.find("{"):raw.rfind("}")+1])
class State(TypedDict,total=False): text:str; image_path:str|None; user_id:str; issue_type:str; description:str; severity:str; severity_score:float; department:str; credits:int; duplicate:bool; duplicate_of:str|None; ticket_id:str
async def n_ai(x):
    a=ai_analyze(x.get("text","") ,x.get("image_path")); return a
async def n_route(x): return {"department":DEPTS.get(x["issue_type"],"Municipality"),"credits":CREDITS.get(x["issue_type"],10)}
async def n_dup(x):
    docs=await db.complaints.find({"issue_type":x["issue_type"],"status":{"$ne":"RESOLVED"}},{"ticket_id":1,"text":1}).limit(80).to_list(80); words=set(x.get("text","").lower().split()); best=None
    for d in docs:
        ow=set((d.get("text","") or "").lower().split()); sim=len(words&ow)/max(1,len(words|ow))
        if sim>=.65: best=d; break
    return {"duplicate":bool(best),"duplicate_of":best.get("ticket_id") if best else None,"credits":0 if best else x["credits"]}
async def n_save(x):
    now=datetime.now(timezone.utc); ticket=f"CIV-{now.strftime('%Y%m%d')}-{str(await db.complaints.count_documents({})+1).zfill(4)}"; doc={**x,"ticket_id":ticket,"status":"ASSIGNED","created_at":now,"updated_at":now}; await db.complaints.insert_one(doc); await db.users.update_one({"_id":x["user_id"]},{"$inc":{"credits":x["credits"]}}); return {"ticket_id":ticket}
g=StateGraph(State); g.add_node("ai_analysis",n_ai); g.add_node("routing",n_route); g.add_node("duplicate_check",n_dup); g.add_node("save",n_save); g.add_edge(START,"ai_analysis"); g.add_edge("ai_analysis","routing"); g.add_edge("routing","duplicate_check"); g.add_edge("duplicate_check","save"); g.add_edge("save",END); GRAPH=g.compile()

def verify(text,msg):
    if not s.openai_api_key: return {"verified":bool(msg.strip()),"score":.88 if msg.strip() else .2,"reason":"Authority evidence received."}
    c=OpenAI(api_key=s.openai_api_key); r=c.responses.create(model=s.openai_model,input=f'''Verify this civic resolution. Return ONLY JSON: verified boolean, score 0-1, reason. Complaint: {text}. Authority response: {msg}'''); raw=r.output_text.replace("```json","").replace("```",""); return json.loads(raw[raw.find("{"):raw.rfind("}")+1])

@app.post("/api/complaints")
async def create(text:str=Form(""),latitude:float|None=Form(None),longitude:float|None=Form(None),image:UploadFile|None=File(None),audio:UploadFile|None=File(None),u=Depends(user)):
    ip=None
    if image and image.filename:
        ip=str(MEDIA/f"{uuid.uuid4().hex}{Path(image.filename).suffix or '.jpg'}"); open(ip,"wb").write(await image.read())
    if not text.strip() and audio and audio.filename:
        ap=str(MEDIA/f"{uuid.uuid4().hex}{Path(audio.filename).suffix or '.webm'}"); open(ap,"wb").write(await audio.read())
        if s.openai_api_key:
            with open(ap,"rb") as f: text=OpenAI(api_key=s.openai_api_key).audio.transcriptions.create(model=s.openai_transcribe_model,file=f).text
    if not text.strip() and not ip: raise HTTPException(400,"Add text, voice, or photo")
    r=await GRAPH.ainvoke({"user_id":u["_id"],"text":text.strip(),"image_path":ip,"latitude":latitude,"longitude":longitude}); return clean(await db.complaints.find_one({"ticket_id":r["ticket_id"]}))
@app.get("/api/complaints")
async def my(u=Depends(user)): return [clean(x) for x in await db.complaints.find({"user_id":u["_id"]}).sort("created_at",-1).to_list(100)]
@app.get("/api/authority/tickets")
async def tickets(u=Depends(authority)): return [clean(x) for x in await db.complaints.find({}).sort("created_at",-1).to_list(200)]
@app.post("/api/authority/tickets/{tid}/start")
async def start(tid:str,u=Depends(authority)): await db.complaints.update_one({"ticket_id":tid},{"$set":{"status":"IN_PROGRESS","updated_at":datetime.now(timezone.utc)}}); return {"ok":True}
@app.post("/api/authority/tickets/{tid}/complete")
async def complete(tid:str,message:str=Form(...),image:UploadFile|None=File(None),u=Depends(authority)):
    d=await db.complaints.find_one({"ticket_id":tid});
    if not d: raise HTTPException(404,"Ticket not found")
    cp=None
    if image and image.filename: cp=str(MEDIA/f"completion-{uuid.uuid4().hex}{Path(image.filename).suffix or '.jpg'}"); open(cp,"wb").write(await image.read())
    v=verify(d.get("text","") ,message); status="RESOLVED" if v.get("verified") else "HUMAN_REVIEW"
    await db.complaints.update_one({"ticket_id":tid},{"$set":{"authority_message":message,"completion_image_path":cp,"resolution_verified":bool(v.get("verified")),"resolution_score":float(v.get("score",0)),"resolution_reason":v.get("reason",""),"status":status,"updated_at":datetime.now(timezone.utc)}}); return {"status":status,"score":v.get("score",0)}
