import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field


TASK_TABLE = "copilot_tasks"
CONV_TABLE = "copilot_conversations"
MSG_TABLE = "copilot_messages"

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
log = logging.getLogger("gep_coopilot")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


PROBLEM_TYPES = {
    "missing_price": "Nem talál árat",
    "wrong_price": "Rossz árat mutat",
    "missing_stock": "Nem talál készletet",
    "error_message": "Hibaüzenet jelent meg",
    "slow_search": "Lassú volt a keresés",
    "other": "Más probléma",
}

STATUS_LABELS = {
    "open": "Nyitott",
    "in_progress": "Folyamatban",
    "resolved": "Megoldva",
}

CHAT_ERROR_FALLBACK = (
    "Még egy chatbot is megérdemel egy kis pihenést. Viccet félre téve, "
    "jelenleg a Gép-Coopilot nem elérhető, dolgozunk a problémán."
)

LOCAL_STORE = Path(__file__).with_name("copilot_local_store.json")

SYSTEM_PROMPT = """Te vagy a Gép-Coopilot, a Price Agent rendszer hibafelvételi asszisztense.
Az elsődleges feladatod eldönteni, hogy a felhasználó valódi, értelmezhető Price Agent / Gép-Coop rendszerhibát ír-e le.
Köszönés, ismételt szó, általános beszélgetés vagy konkrét hiba nélküli szöveg NEM hibabejelentés.
Például "szia", "szia szia szia", "segíts", "nem működik" önmagában nem elég: valid_issue legyen false.
Valid hibabejelentéshez legyen legalább egy konkrét hibajelenség, például nincs ár, rossz ár, nincs készlet, hibaüzenet vagy lassú keresés.
Magyarul beszélj, röviden válaszolj. Ne adj technikai tanácsot, és ne próbáld megoldani a hibát.
Ha a felhasználó üzenetében felismerhető a hiba, webshop és cikkszám, töltsd ki ezeket.
Több webshop esetén a webshops listába mindegyiket tedd bele, a webshop mezőbe pedig vesszővel elválasztva.
Probléma felismerés:
- "nem talál árat", "nincs ár", "üres ár" => missing_price
- "rossz ár", "hibás ár" => wrong_price
- "nem talál készletet", "nincs készlet" => missing_stock
- "hibaüzenet", "error" => error_message
- "lassú", "sokáig tart" => slow_search
Válaszolj csak JSON objektummal:
{
  "assistant_message": "rövid magyar válasz",
  "valid_issue": true|false,
  "problem_type": "missing_price|wrong_price|missing_stock|error_message|slow_search|other|null",
  "webshop": "string|null",
  "webshops": ["string"],
  "product_number": "string|null",
  "description": "string|null",
  "expected_result": "string|null",
  "ready_to_submit": false,
  "out_of_scope": true|false
}"""


class ChatMessage(BaseModel):
    sender: str
    message: str


class CopilotChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    state: dict = Field(default_factory=dict)


class CopilotSubmitRequest(BaseModel):
    problem_type: str
    webshop: str
    product_number: str
    description: str
    expected_result: str | None = None
    summary: str
    messages: list[ChatMessage] = Field(default_factory=list)


class CopilotStatusRequest(BaseModel):
    status: str
    admin_note: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value) -> str:
    return str(value or "").strip()


def _problem_label(problem_type: str | None) -> str:
    return PROBLEM_TYPES.get(problem_type or "", problem_type or "")


def _is_out_of_scope(text: str) -> bool:
    lower = text.lower()
    off_topic = [
        "időjárás", "politika", "vicc", "befektetés", "részvény", "bitcoin",
        "programoz", "python", "recept", "magán", "sport", "film",
    ]
    app_terms = [
        "ár", "készlet", "webshop", "cikkszám", "sku", "ean", "price agent",
        "gepcoop", "gép-coop", "lekérdez", "hiba", "lassú", "találat",
    ]
    return any(w in lower for w in off_topic) and not any(w in lower for w in app_terms)


def _is_meaningful_issue(text: str) -> bool:
    lower = " ".join(text.lower().split())
    if len(lower) < 8:
        return False
    words = re.findall(r"[a-záéíóöőúüű0-9]+", lower)
    if len(set(words)) <= 2 and len(words) >= 2:
        return False
    concrete_patterns = [
        r"(nem talál|nincs|üres).{0,20}ár",
        r"(rossz|hibás).{0,20}ár",
        r"(nem talál|nincs|rossz|hibás).{0,20}készlet",
        r"hibaüzenet",
        r"\berror\b",
        r"(lassú|sokáig|percekig).{0,30}(keres|lekérdez|fut)",
        r"(üres|nincs).{0,20}találat",
    ]
    return any(re.search(pattern, lower) for pattern in concrete_patterns)


def _infer_problem_type(text: str) -> str | None:
    lower = text.lower()
    if "nem talál" in lower and "ár" in lower:
        return "missing_price"
    if "nincs ár" in lower or "üres ár" in lower:
        return "missing_price"
    if "rossz ár" in lower or "hibás ár" in lower:
        return "wrong_price"
    if "készlet" in lower and ("nem" in lower or "hiány" in lower or "rossz" in lower):
        return "missing_stock"
    if "hibaüzenet" in lower or "error" in lower:
        return "error_message"
    if "lassú" in lower or "sokáig" in lower:
        return "slow_search"
    if "hiba" in lower or "probléma" in lower:
        return "other"
    return None


def _infer_product_number(text: str) -> str | None:
    matches = re.findall(r"\b[A-Z0-9][A-Z0-9._/-]{4,}\b", text.upper())
    matches = [m for m in matches if any(ch.isdigit() for ch in m)]
    if not matches:
        return None
    return sorted(matches, key=len, reverse=True)[0]


def _infer_webshop(text: str) -> str | None:
    names = [
        "Gép-Coop", "Csavarda", "Iron Trade", "Koelner", "Mekrs", "Fabory",
        "Reyher", "Hopefix", "Fastbolt", "Schäfer", "Schaefer", "King B2B",
        "Wasishop", "Argip", "Inoxmare", "Vipa",
    ]
    lower = text.lower()
    for name in names:
        if name.lower() in lower:
            return name
    return None


def _merge_state(old: dict, new: dict) -> dict:
    merged = dict(old or {})
    for key in ("problem_type", "webshop", "product_number", "description", "expected_result"):
        value = new.get(key)
        if value not in (None, "", "null"):
            merged[key] = value
    return merged


def _fallback_chat(message: str, state: dict) -> dict:
    if _is_out_of_scope(message):
        return {
            "assistant_message": "Ebben sajnos nem tudok segíteni. A költségek csökkentése érdekében csak a rendszer működésével kapcsolatos hibák felvételében tudok segíteni.",
            "out_of_scope": True,
            "ready_to_submit": False,
            **{k: state.get(k) for k in ("problem_type", "webshop", "product_number", "description", "expected_result")},
        }

    if not _is_meaningful_issue(message):
        return {
            "assistant_message": "Kérlek, írd le röviden, milyen konkrét hibát tapasztaltál a rendszer használata közben.",
            "valid_issue": False,
            "out_of_scope": False,
            "ready_to_submit": False,
            **{k: state.get(k) for k in ("problem_type", "webshop", "product_number", "description", "expected_result")},
        }

    inferred = {
        "problem_type": _infer_problem_type(message),
        "webshop": _infer_webshop(message),
        "product_number": _infer_product_number(message),
    }
    current = _merge_state(state, inferred)
    current["valid_issue"] = True

    if not current.get("description") and len(message.strip()) > 8:
        current["description"] = message.strip()

    missing = [k for k in ("problem_type", "webshop", "product_number", "description") if not current.get(k)]
    if missing:
        questions = {
            "problem_type": "Milyen probléma történt?",
            "webshop": "Melyik webshopnál jelentkezett?",
            "product_number": "Melyik cikkszámmal kerestél?",
            "description": "Mi történt pontosan?",
        }
        current["assistant_message"] = questions[missing[0]]
        current["ready_to_submit"] = False
        current["out_of_scope"] = False
        return current

    summary = _build_summary(current)
    current["assistant_message"] = f"Rögzítem így?\n\n{summary}"
    current["ready_to_submit"] = True
    current["out_of_scope"] = False
    return current


def _build_summary(data: dict) -> str:
    lines = [
        f"Probléma: {_problem_label(data.get('problem_type'))}",
        f"Webshop: {_clean(data.get('webshop'))}",
        f"Cikkszám: {_clean(data.get('product_number'))}",
        f"Történés: {_clean(data.get('description'))}",
    ]
    if _clean(data.get("expected_result")):
        lines.append(f"Elvárt eredmény: {_clean(data.get('expected_result'))}")
    return "\n".join(lines)


async def _openai_chat(message: str, history: list[ChatMessage], state: dict) -> dict | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    compact_history = [
        {"role": "assistant" if m.sender == "assistant" else "user", "content": m.message[:600]}
        for m in history[-8:]
        if m.sender in {"assistant", "user"}
    ]
    user_payload = {
        "current_state": state or {},
        "latest_user_message": message,
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *compact_history,
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if res.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"OpenAI hiba: {res.text[:200]}")
    content = res.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="OpenAI válasz nem értelmezhető JSON-ként.") from exc


def _insert_task(sb, username: str, req: CopilotSubmitRequest) -> dict:
    now = _now()
    task_id = secrets.token_hex(6)
    payload = {
        "id": task_id,
        "customer_id": "gepcoop",
        "user_id": username,
        "title": "Gép-Coop árlekérdezési hiba",
        "problem_type": req.problem_type,
        "webshop": req.webshop,
        "product_number": req.product_number,
        "description": req.description,
        "expected_result": req.expected_result,
        "summary": req.summary,
        "status": "open",
        "created_at": now,
        "updated_at": now,
    }
    sb.table(TASK_TABLE).insert(payload).execute()

    conv_id = secrets.token_hex(8)
    sb.table(CONV_TABLE).insert({
        "id": conv_id,
        "task_id": task_id,
        "customer_id": "gepcoop",
        "user_id": username,
        "created_at": now,
    }).execute()

    rows = [
        {
            "id": secrets.token_hex(8),
            "conversation_id": conv_id,
            "sender": m.sender,
            "message": m.message,
            "created_at": now,
        }
        for m in req.messages
        if _clean(m.message)
    ]
    if rows:
        sb.table(MSG_TABLE).insert(rows).execute()
    return {**payload, "conversation_id": conv_id}


def _local_read() -> dict:
    if not LOCAL_STORE.exists():
        return {"tasks": [], "conversations": [], "messages": []}
    try:
        return json.loads(LOCAL_STORE.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": [], "conversations": [], "messages": []}


def _local_write(data: dict) -> None:
    LOCAL_STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _local_insert_task(username: str, req: CopilotSubmitRequest) -> dict:
    data = _local_read()
    now = _now()
    task_id = secrets.token_hex(6)
    conv_id = secrets.token_hex(8)
    task = {
        "id": task_id,
        "customer_id": "gepcoop",
        "user_id": username,
        "title": "Gép-Coop árlekérdezési hiba",
        "problem_type": req.problem_type,
        "webshop": req.webshop,
        "product_number": req.product_number,
        "description": req.description,
        "expected_result": req.expected_result,
        "summary": req.summary,
        "status": "open",
        "admin_note": "",
        "created_at": now,
        "updated_at": now,
    }
    data.setdefault("tasks", []).append(task)
    data.setdefault("conversations", []).append({
        "id": conv_id,
        "task_id": task_id,
        "customer_id": "gepcoop",
        "user_id": username,
        "created_at": now,
    })
    for m in req.messages:
        if _clean(m.message):
            data.setdefault("messages", []).append({
                "id": secrets.token_hex(8),
                "conversation_id": conv_id,
                "sender": m.sender,
                "message": m.message,
                "created_at": now,
            })
    _local_write(data)
    return {**task, "conversation_id": conv_id, "storage": "local"}


def _local_tasks() -> list[dict]:
    return sorted(_local_read().get("tasks", []), key=lambda r: r.get("created_at", ""), reverse=True)


def _local_detail(task_id: str) -> dict | None:
    data = _local_read()
    task = next((t for t in data.get("tasks", []) if t.get("id") == task_id), None)
    if not task:
        return None
    conv = next((c for c in data.get("conversations", []) if c.get("task_id") == task_id), None)
    messages = []
    if conv:
        messages = [m for m in data.get("messages", []) if m.get("conversation_id") == conv.get("id")]
    return {"task": task, "conversation": conv, "messages": messages}


def _local_update_status(task_id: str, status: str, admin_note: str | None) -> dict | None:
    data = _local_read()
    for task in data.get("tasks", []):
        if task.get("id") == task_id:
            task["status"] = status
            task["updated_at"] = _now()
            if admin_note is not None:
                task["admin_note"] = admin_note
            _local_write(data)
            return task
    return None


def create_copilot_router(
    get_supabase: Callable,
    get_username: Callable,
    get_admin: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/copilot", tags=["Gép-Coopilot"])

    @router.get("/config")
    def config():
        return {"enabled": _env_bool("COPILOT_ENABLED", False)}

    @router.post("/chat")
    async def chat(req: CopilotChatRequest, authorization: str | None = Header(default=None)):
        get_username(authorization)
        message = _clean(req.message)
        if not message:
            return {
                "assistant_message": CHAT_ERROR_FALLBACK,
                "ready_to_submit": False,
                "out_of_scope": False,
                "summary": "",
                "model": "error_fallback",
            }
        try:
            model_result = await _openai_chat(message, req.history, req.state)
            data = model_result or _fallback_chat(message, req.state)
            data["valid_issue"] = bool(data.get("valid_issue", False))
            if not data["valid_issue"]:
                data["ready_to_submit"] = False
                data["summary"] = ""
                data["model"] = MODEL if model_result else "fallback"
                return data
            merged = _merge_state(req.state, data)
            for key, value in merged.items():
                data.setdefault(key, value)
            data["summary"] = _build_summary(data) if data.get("ready_to_submit") else ""
            data["model"] = MODEL if model_result else "fallback"
            return data
        except Exception:
            log.exception("Gép-Coopilot chat hiba")
            return {
                "assistant_message": CHAT_ERROR_FALLBACK,
                "ready_to_submit": False,
                "out_of_scope": False,
                "summary": "",
                "model": "error_fallback",
            }

    @router.post("/tasks")
    def submit_task(req: CopilotSubmitRequest, authorization: str | None = Header(default=None)):
        username = get_username(authorization)
        if req.problem_type not in PROBLEM_TYPES:
            raise HTTPException(status_code=400, detail="Érvénytelen probléma típus.")
        if not req.webshop or not req.product_number or not req.description:
            raise HTTPException(status_code=400, detail="Hiányzó kötelező mező.")
        sb = get_supabase()
        if sb is None:
            row = _local_insert_task(username, req)
            row["warning"] = "Supabase nincs konfigurálva, lokális fallback mentés történt."
            return {"task": row, "display_id": f"#{row['id'][:6].upper()}"}
        try:
            row = _insert_task(sb, username, req)
        except Exception as exc:
            row = _local_insert_task(username, req)
            row["warning"] = f"Supabase tábla nem elérhető, lokális fallback mentés történt: {exc}"
        return {"task": row, "display_id": f"#{row['id'][:6].upper()}"}

    @router.get("/admin/tasks")
    def admin_tasks(authorization: str | None = Header(default=None)):
        get_admin(authorization)
        sb = get_supabase()
        if sb is None:
            return {"tasks": _local_tasks(), "status_labels": STATUS_LABELS, "problem_labels": PROBLEM_TYPES, "storage": "local"}
        try:
            res = sb.table(TASK_TABLE).select("*").order("created_at", desc=True).limit(200).execute()
        except Exception as exc:
            return {"tasks": _local_tasks(), "status_labels": STATUS_LABELS, "problem_labels": PROBLEM_TYPES, "storage": "local", "warning": str(exc)}
        return {"tasks": res.data or [], "status_labels": STATUS_LABELS, "problem_labels": PROBLEM_TYPES}

    @router.get("/admin/tasks/{task_id}")
    def admin_task_detail(task_id: str, authorization: str | None = Header(default=None)):
        get_admin(authorization)
        sb = get_supabase()
        if sb is None:
            detail = _local_detail(task_id)
            if not detail:
                raise HTTPException(status_code=404, detail="Hibajegy nem található.")
            return detail
        try:
            task = (sb.table(TASK_TABLE).select("*").eq("id", task_id).limit(1).execute().data or [None])[0]
        except Exception:
            detail = _local_detail(task_id)
            if not detail:
                raise HTTPException(status_code=404, detail="Hibajegy nem található.")
            return detail
        if not task:
            raise HTTPException(status_code=404, detail="Hibajegy nem található.")
        convs = sb.table(CONV_TABLE).select("*").eq("task_id", task_id).limit(1).execute().data or []
        messages = []
        if convs:
            messages = sb.table(MSG_TABLE).select("*").eq("conversation_id", convs[0]["id"]).order("created_at").execute().data or []
        return {"task": task, "conversation": convs[0] if convs else None, "messages": messages}

    @router.post("/admin/tasks/{task_id}/status")
    def admin_update_status(task_id: str, req: CopilotStatusRequest, authorization: str | None = Header(default=None)):
        get_admin(authorization)
        if req.status not in STATUS_LABELS:
            raise HTTPException(status_code=400, detail="Érvénytelen státusz.")
        sb = get_supabase()
        if sb is None:
            task = _local_update_status(task_id, req.status, req.admin_note)
            if not task:
                raise HTTPException(status_code=404, detail="Hibajegy nem található.")
            return {"task": task, "storage": "local"}
        payload = {"status": req.status, "updated_at": _now()}
        if req.admin_note is not None:
            payload["admin_note"] = req.admin_note
        try:
            res = sb.table(TASK_TABLE).update(payload).eq("id", task_id).execute()
            return {"task": (res.data or [None])[0]}
        except Exception:
            task = _local_update_status(task_id, req.status, req.admin_note)
            if not task:
                raise HTTPException(status_code=404, detail="Hibajegy nem található.")
            return {"task": task, "storage": "local"}

    return router
