from fastapi import APIRouter, HTTPException, Response
import logging
from services.top_k_shadow_view import load_shadow_view

router = APIRouter(prefix='/api', tags=['Shadow ranking'])
logger = logging.getLogger(__name__)


@router.get('/top-k-shadow')
def top_k_shadow(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return load_shadow_view()
    except Exception:
        logger.exception('read-only shadow view failed')
        raise HTTPException(status_code=503, detail='Shadow 資料暫時無法讀取，請稍後重試。') from None
