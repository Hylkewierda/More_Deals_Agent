import os
import re
import json
import asyncio
from datetime import datetime
from typing import List, Optional, AsyncGenerator

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import anthropic
from docx import Document
from pptx import Presentation
from dotenv import load_dotenv
import io

load_dotenv()

app = FastAPI(title="More Deals Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Knowledge base ──────────────────────────────────────────────────────────

DOCS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DOCS_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

KNOWLEDGE_FILES = [
    ("MORE DEALS WEEK 1 - SALES FUNDAMENTALS.pptx", "Week 1 – Sales Fundamentals"),
    ("MORE DEALS WEEK 2 - FUNNEL BUILDING.pptx", "Week 2 – Funnel Building"),
    ("MORE DEALS WEEK 3 - SELLING.pptx", "Week 3 – Selling"),
    ("MORE DEALS WEEK 4 - NEGOTIATING.pptx", "Week 4 – Negotiating"),
    ("MORE DEALS WEEK 5 - CLOSING.pptx", "Week 5 – Closing"),
    ("sales_workshop.docx", "Sales Workshop – Verkopen & Onderhandelen voor Startups"),
    ("sales_workshop_2.docx", "Sales Workshop 2 – Eerste Salesgesprekken & Lead Generatie"),
    ("negotiation_workshop.docx", "Onderhandelen Workshop – Week 4"),
    ("fundraising_workshop.docx", "Fundraising Workshop – Fundraising voor Startups"),
]


def _read_docx(filepath: str) -> str:
    doc = Document(filepath)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _read_pptx(filepath: str) -> str:
    prs = Presentation(filepath)
    lines = []
    for slide_num, slide in enumerate(prs.slides, 1):
        slide_texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        slide_texts.append(text)
        if slide_texts:
            lines.append(f"[Slide {slide_num}]")
            lines.extend(slide_texts)
            lines.append("")
    return "\n".join(lines)


def load_knowledge_base() -> str:
    sections = []
    for filename, title in KNOWLEDGE_FILES:
        filepath = os.path.join(DOCS_DIR, filename)
        if not os.path.exists(filepath):
            print(f"WARN: Niet gevonden: {filename}")
            continue
        try:
            if filename.lower().endswith(".pptx"):
                text = _read_pptx(filepath)
            else:
                text = _read_docx(filepath)
            sections.append(f"### {title}\n\n{text}")
        except Exception as e:
            print(f"ERROR: Fout bij laden {filename}: {e}")
    return "\n\n---\n\n".join(sections)


print("Kennisbasis laden…")
KNOWLEDGE_BASE = load_knowledge_base()
print(f"Kennisbasis geladen: {len(KNOWLEDGE_BASE):,} tekens")

SYSTEM_INSTRUCTIONS = """Je bent de **More Deals AI Agent** — een expert sales coach en assistent van het More Deals programma, opgericht door Joey Moreau.

More Deals helpt ondernemers en startups om hun salescijfers te verhogen via een gestructureerd programma dat bestaat uit:
- Week 1: Sales Fundamentals
- Week 2: Funnel Building
- Week 3: Selling (LEADS framework)
- Week 4: Negotiating
- Week 5: Closing

**Jouw rol:**
- Beantwoord vragen over sales, onderhandelen, lead generatie en fundraising
- Geef praktisch, concreet advies op basis van het More Deals programma
- Leg frameworks uit (LEADS, MAGIC, Feedback Loop, Certainty Script, etc.)
- Help bij het kwalificeren van leads en deals
- Spreek Nederlands tenzij de gebruiker Engels spreekt

**Communicatiestijl:**
- Direct en to-the-point, zoals Joey Moreau dat doet
- Gebruik concrete voorbeelden en scripts
- Geef altijd actionable stappen
- Wees enthousiast maar professioneel
- Gebruik de frameworks en taal uit de trainingen

**Formatting regels:**
- Gebruik NOOIT emoji's in je antwoorden. Geen enkele emoji, nergens.
- Structureer antwoorden duidelijk met markdown: koppen (##, ###), bullet points, genummerde lijsten en **vetgedrukte** termen
- Gebruik witregels tussen secties voor leesbaarheid
- Houd alinea's kort en scanbaar (max 3-4 zinnen per alinea)
- Zet frameworks, methodes en belangrijke termen altijd **vetgedrukt**

Hieronder staat je volledige kennisbasis. Gebruik deze als bron voor al je antwoorden."""

SYSTEM_PROMPT_BLOCKS = [
    {"type": "text", "text": SYSTEM_INSTRUCTIONS},
    {
        "type": "text",
        "text": f"## KENNISBASIS – MORE DEALS PROGRAMMA\n\n{KNOWLEDGE_BASE}",
        "cache_control": {"type": "ephemeral"},
    },
]

# ── Anthropic client ────────────────────────────────────────────────────────

def get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY niet ingesteld. Voeg hem toe aan je .env bestand."
        )
    return anthropic.Anthropic(api_key=api_key)


# ── Pydantic models ─────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]


class LeadForm(BaseModel):
    naam: str
    bedrijf: str
    email: str
    telefoon: Optional[str] = None
    sector: Optional[str] = None
    bedrijfsgrootte: Optional[str] = None
    pijnpunten: str
    budget: Optional[str] = None
    tijdlijn: Optional[str] = None
    notities: Optional[str] = None


class DealForm(BaseModel):
    prospect_naam: str
    bedrijf: str
    email: Optional[str] = None
    deal_waarde: Optional[str] = None
    fase: str  # prospecting, gekwalificeerd, voorstel, onderhandeling, closing, gewonnen, verloren
    volgende_stap: str
    follow_up_datum: Optional[str] = None
    notities: Optional[str] = None


# ── Query routing ──────────────────────────────────────────────────────────

SALES_KEYWORDS = re.compile(
    r"\b("
    r"sales|lead|leads|funnel|pitch|deal|deals|onderhandel|closing|close"
    r"|prospect|follow.?up|offerte|klant|verkoop|verkopen|more deals"
    r"|leads framework|magic|certainty script|feedback loop"
    r"|bezwaar|bezwaren|objecit|prijs|korting|marge"
    r"|pipeline|kwalificer|acquisitie|cold.?call|cold.?mail"
    r"|voorstel|contract|omzet|target|quota|conversie"
    r"|fundrais|investor|investeerder|startup|seed|raise"
    r"|week [1-5]|workshop"
    r")\b",
    re.IGNORECASE,
)


def needs_knowledge_base(query: str) -> bool:
    return bool(SALES_KEYWORDS.search(query))


# ── Chat endpoint (streaming) ───────────────────────────────────────────────

@app.post("/api/chat")
async def chat(request: ChatRequest):
    client = get_client()

    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    last_user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
    use_kb = needs_knowledge_base(last_user_msg)
    system = SYSTEM_PROMPT_BLOCKS if use_kb else SYSTEM_INSTRUCTIONS

    async def generate() -> AsyncGenerator[str, None]:
        try:
            if use_kb:
                steps = [
                    "Vraag analyseren...",
                    "Relevante context ophalen...",
                    "Antwoord formuleren...",
                ]
                for step in steps:
                    yield f"data: {json.dumps({'status': step})}\n\n"
                    await asyncio.sleep(0.6)

            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=system,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': f'**Fout van Anthropic API:** {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── File Extraction Endpoint ────────────────────────────────────────────────

@app.post("/api/extract")
async def extract_text(file: UploadFile = File(...)):
    name = file.filename.lower()
    content = await file.read()
    text = ""
    try:
        if name.endswith(".docx"):
            doc = Document(io.BytesIO(content))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        elif name.endswith((".txt", ".md", ".csv", ".json")):
            text = content.decode("utf-8")
        else:
            return {"error": "Unsupported file type. Please use .txt, .md, .csv, .json, or .docx."}
    except Exception as e:
        return {"error": f"Failed to parse file: {str(e)}"}
    
    return {"text": text, "filename": file.filename}

# ── Lead endpoints ──────────────────────────────────────────────────────────

def load_json(filename: str) -> list:
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(filename: str, data: list) -> None:
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.post("/api/leads")
async def create_lead(lead: LeadForm):
    leads = load_json("leads.json")
    entry = lead.model_dump()
    entry["id"] = len(leads) + 1
    entry["aangemaakt_op"] = datetime.now().isoformat()
    leads.append(entry)
    save_json("leads.json", leads)
    return {"success": True, "id": entry["id"], "bericht": f"Lead '{lead.naam}' succesvol opgeslagen!"}


@app.get("/api/leads")
async def get_leads():
    return load_json("leads.json")


@app.post("/api/deals")
async def create_deal(deal: DealForm):
    deals = load_json("deals.json")
    entry = deal.model_dump()
    entry["id"] = len(deals) + 1
    entry["aangemaakt_op"] = datetime.now().isoformat()
    deals.append(entry)
    save_json("deals.json", deals)
    return {"success": True, "id": entry["id"], "bericht": f"Deal '{deal.prospect_naam}' succesvol opgeslagen!"}


@app.get("/api/deals")
async def get_deals():
    return load_json("deals.json")


# ── Static files & root ────────────────────────────────���────────────────────

app.mount("/static", StaticFiles(directory=os.path.join(DOCS_DIR, "static")), name="static")


@app.get("/")
async def root():
    response = FileResponse(os.path.join(DOCS_DIR, "static", "index.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/health")
async def health():
    return {"status": "ok", "knowledge_chars": len(KNOWLEDGE_BASE)}
