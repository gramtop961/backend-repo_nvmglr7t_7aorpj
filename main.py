import os
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def rpc_call(method: str, params: Optional[List[Any]] = None) -> Any:
    url = SOLANA_RPC_URL
    headers = {"Content-Type": "application/json"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"RPC request error: {str(e)}")
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise HTTPException(status_code=502, detail=f"RPC error: {err}")
    return data.get("result")


@app.get("/")
def read_root():
    return {"message": "Hello from FastAPI Backend!"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        from database import db  # type: ignore
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = getattr(db, 'name', "✅ Connected")
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:  # noqa: BLE001
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:  # noqa: BLE001
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    response["solana_rpc"] = SOLANA_RPC_URL
    return response


@app.get("/api/solana/stats")
def solana_stats():
    """Return basic Solana network stats derived from RPC."""
    # Slot
    slot = rpc_call("getSlot")
    # TPS estimate using recent performance samples (last 1-5 minutes)
    samples = rpc_call("getRecentPerformanceSamples", [30]) or []
    tps: Optional[float] = None
    if samples:
        # Average over last N samples
        tx = sum(s.get("numTransactions", 0) for s in samples)
        secs = sum(s.get("samplePeriodSecs", 0) for s in samples)
        tps = round(tx / secs, 2) if secs else None
    # Validators count
    vote_accounts = rpc_call("getVoteAccounts") or {}
    current = vote_accounts.get("current", [])
    delinquent = vote_accounts.get("delinquent", [])
    validators = len(current) + len(delinquent)

    # Recent blocks (approx by block height diff in last 5 minutes)
    height1 = rpc_call("getBlockHeight")
    time.sleep(0.05)
    height2 = rpc_call("getBlockHeight")
    recent_blocks = max(0, (height2 or height1) - (height1 or 0))

    return {
        "tps": tps,
        "slot": slot,
        "validators": validators,
        "recentBlocks": recent_blocks,
    }


def _program_label(tx: Dict[str, Any]) -> str:
    try:
        # Try to infer main program from first instruction of first message
        message = tx.get("transaction", {}).get("message", {})
        instructions = message.get("instructions", [])
        if instructions:
            prog = instructions[0].get("programId") or instructions[0].get("programIdIndex")
            if isinstance(prog, str):
                return prog
        # Fallback to meta status
        ok = tx.get("meta", {}).get("err") is None
        return "Success" if ok else "Error"
    except Exception:  # noqa: BLE001
        return "Transaction"


@app.get("/api/solana/recent-transactions")
def recent_transactions(limit: int = Query(10, ge=1, le=20)):
    """Return recent transactions by sampling signatures of the System Program (busy account)."""
    system_program = "11111111111111111111111111111111"
    sigs = rpc_call("getSignaturesForAddress", [system_program, {"limit": limit}]) or []
    if not sigs:
        return {"items": []}

    # Fetch detailed transactions for a subset (limit) to get fee, slot, program label
    items: List[Dict[str, Any]] = []
    for s in sigs[:limit]:
        signature = s.get("signature")
        tx = rpc_call("getTransaction", [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        fee_lamports = (tx.get("meta") or {}).get("fee", 0)
        fee_sol = round(fee_lamports / 1_000_000_000, 9)
        items.append({
            "sig": signature,
            "slot": tx.get("slot"),
            "fee": fee_sol,
            "type": _program_label(tx),
        })

    return {"items": items}


@app.get("/api/solana/search")
def search(q: str = Query(..., min_length=2)):
    """Basic search helper: detects signature, address, or slot and returns minimal info."""
    q_stripped = q.strip()

    # Try slot number
    if q_stripped.isdigit():
        slot = int(q_stripped)
        try:
            block = rpc_call("getBlock", [slot, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
        except HTTPException:
            block = None
        return {"kind": "slot", "slot": slot, "txCount": len(block.get("transactions", [])) if block else 0}

    # Try signature (base58 length often 87-88)
    if 80 <= len(q_stripped) <= 100:
        try:
            tx = rpc_call("getTransaction", [q_stripped, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
        except HTTPException:
            tx = None
        if tx:
            fee = ((tx.get("meta") or {}).get("fee", 0)) / 1_000_000_000
            return {"kind": "signature", "signature": q_stripped, "slot": tx.get("slot"), "fee": fee, "ok": (tx.get("meta") or {}).get("err") is None}

    # Assume address: return balance and latest signatures
    try:
        bal = rpc_call("getBalance", [q_stripped])
        sigs = rpc_call("getSignaturesForAddress", [q_stripped, {"limit": 5}])
        return {
            "kind": "address",
            "address": q_stripped,
            "balance": (bal or 0) / 1_000_000_000,
            "signatures": [s.get("signature") for s in (sigs or [])],
        }
    except HTTPException:
        raise HTTPException(status_code=404, detail="Not found or invalid query")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
