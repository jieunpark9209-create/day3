"""
main.py — 제주 미세먼지 백엔드 (FastAPI)
  공공데이터 API ─① 받기→ FastAPI ─② 가공 ─③ 예측→ ④ Supabase 저장
  프론트(Vercel/로컬) ── 결과만 요청 ──┘  (키는 전부 서버 안에만)

로컬: .env 를 읽음 / Railway: Variables 가 환경변수로 들어옴
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

import collect  # noqa: E402  (load_dotenv 뒤에 import)
import db  # noqa: E402
import predict  # noqa: E402

log = logging.getLogger("uvicorn.error")

DATA_KEY = os.getenv("DATA_GO_KR_KEY", "").strip()
SIDO = os.getenv("SIDO_NAME", "제주").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEMO = os.getenv("DEMO_MODE", "0") == "1" or not DATA_KEY
SOURCE = "demo" if DEMO else "airkorea"
AUTO = os.getenv("AUTO_COLLECT", "1") == "1"
MINUTES = int(os.getenv("COLLECT_MINUTES", "60"))
FRONT_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONT_ORIGINS", "").split(",")
                 if o.strip()]

store = db.get_store()


last_error: str | None = None  # 마지막 수집 실패 이유 (/health 에서 확인)


def run_collect(hours: int = 1) -> dict:
    global last_error
    try:
        items = collect.fake_raw(hours) if DEMO else collect.fetch_raw(SIDO, DATA_KEY)
        rows, stats = collect.clean(items, SIDO, SOURCE)
        saved = store.save_readings(rows)
        entry = {"ran_at": datetime.now(timezone.utc).isoformat(), "source": SOURCE,
                 **stats, "saved": saved}
        store.save_log(entry)
    except Exception as e:
        last_error = f"{datetime.now(timezone.utc).isoformat()} {e}"[:400]
        raise
    last_error = None
    log.info("collect %s", entry)
    return entry


def seed_demo(hours: int = 72) -> None:
    """예측 체험용: 연습 데이터(source='demo') 72시간치를 한 번 채운다.
    실제 데이터(airkorea)와 source 컬럼이 달라서 섞이지 않는다."""
    rows, stats = collect.clean(collect.fake_raw(hours), SIDO, "demo")
    saved = store.save_readings(rows)
    store.save_log({"ran_at": datetime.now(timezone.utc).isoformat(), "source": "demo",
                    **stats, "saved": saved})


def pick_source(source: str | None) -> str:
    if source is None:
        return SOURCE
    if source not in ("demo", "airkorea"):
        raise HTTPException(400, "source 는 demo 또는 airkorea")
    return source


async def auto_loop():
    """서버가 켜져 있는 동안 COLLECT_MINUTES마다 자동 수집"""
    if not store.recent("demo", 1):  # 연습 데이터가 없으면 한 번 채운다
        try:
            await asyncio.to_thread(seed_demo)
        except Exception:
            log.exception("연습 데이터 채우기 실패")
    while True:
        try:
            await asyncio.to_thread(run_collect)
        except Exception:
            log.error("자동 수집 실패: %s", last_error)
        await asyncio.sleep(MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(auto_loop()) if AUTO else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="제주 미세먼지 백엔드", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=FRONT_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def home():
    return {"service": "제주 미세먼지 백엔드", "mode": SOURCE, "docs": "/docs"}


@app.get("/health")
def health():
    missing = [k for k in ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "DATA_GO_KR_KEY",
                           "ADMIN_TOKEN", "FRONT_ORIGINS"] if not os.getenv(k, "").strip()]
    return {"ok": True, "mode": SOURCE, "store": store.kind,
            "auto_collect_minutes": MINUTES if AUTO else None, "missing": missing,
            "last_error": last_error}


@app.post("/collect")
def collect_now(x_admin_token: str = Header(default=""),
                hours: int = Query(1, ge=1, le=168, description="연습 모드에서만: 몇 시간치 만들지")):
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN 을 먼저 설정하세요")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "x-admin-token 이 틀렸습니다")
    try:
        return run_collect(hours if DEMO else 1)
    except Exception as e:
        raise HTTPException(502, str(e))


def _latest_rows(source: str):
    rows = store.recent(source)
    seen, latest = set(), []
    for r in rows:  # 시각 내림차순 → 측정소별 첫 행이 최신
        if r["station"] not in seen:
            seen.add(r["station"])
            latest.append(r)
    return latest


@app.get("/air/latest")
def air_latest(source: str | None = None):
    rows = _latest_rows(pick_source(source))
    rows.sort(key=lambda r: r["pm10"] or 0, reverse=True)
    return [{"station": r["station"], "time": db.kst(r["data_time"]),
             "pm10": r["pm10"], "pm10_grade": r["pm10_grade"],
             "pm25": r["pm25"], "pm25_grade": r["pm25_grade"]} for r in rows]


@app.get("/air/summary")
def air_summary(source: str | None = None):
    source = pick_source(source)
    rows = _latest_rows(source)
    pm10s = [r["pm10"] for r in rows if r["pm10"] is not None]
    pm25s = [r["pm25"] for r in rows if r["pm25"] is not None]
    worst = max(rows, key=lambda r: r["pm10"] or 0) if rows else None
    logs = store.logs(1)
    avg10 = round(sum(pm10s) / len(pm10s)) if pm10s else None
    return {
        "mode": source,
        "stations": len(rows),
        "avg_pm10": avg10,
        "avg_pm25": round(sum(pm25s) / len(pm25s)) if pm25s else None,
        "avg_pm10_grade": collect.grade_pm10(avg10) if avg10 is not None else None,
        "worst_station": worst and {"station": worst["station"], "pm10": worst["pm10"]},
        "last_collect": logs[0] if logs else None,
    }


@app.get("/air/predict")
def air_predict(station: str, source: str | None = None):
    hist = store.history(pick_source(source), station)
    if not hist:
        raise HTTPException(404, f"'{station}' 측정소 데이터가 없습니다")
    return {"station": station, **predict.predict_next(hist)}


@app.get("/logs")
def logs():
    return store.logs()
