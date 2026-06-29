from fastapi import APIRouter, HTTPException

from models.draw import DrawUpload
from db import insert_draw

router = APIRouter(prefix="/api", tags=["draws"])


@router.post("/upload_draws")
def upload_draws(payload: DrawUpload):
    if len(set(payload.numbers)) != 20:
        raise HTTPException(status_code=400, detail="號碼不可重複")

    if any(n < 1 or n > 80 for n in payload.numbers):
        raise HTTPException(status_code=400, detail="號碼必須在 1~80")

    insert_draw(
        issue=payload.issue,
        time_text=payload.time,
        numbers=payload.numbers,
        super_number=payload.super_number,
    )

    return {
        "status": "ok",
        "message": "已寫入資料庫",
        "issue": payload.issue,
    }