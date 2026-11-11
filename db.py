"""
db.py — ④ 저장: Supabase(진짜 DB)
SUPABASE_URL / SUPABASE_SECRET_KEY 가 있으면 Supabase, 없으면 메모리 임시 저장.
"""
import os
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def kst(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(KST).strftime("%Y-%m-%d %H:%M")


class SupabaseStore:
    kind = "supabase"

    def __init__(self, url, key):
        from supabase import create_client
        self.sb = create_client(url, key)

    def save_readings(self, rows):
        if not rows:
            return 0
        res = (self.sb.table("air_readings")
               .upsert(rows, on_conflict="source,station,data_time").execute())
        return len(res.data or [])

    def save_log(self, log):
        self.sb.table("collect_logs").insert(log).execute()

    def recent(self, source, limit=300):
        res = (self.sb.table("air_readings").select("*").eq("source", source)
               .order("data_time", desc=True).limit(limit).execute())
        return res.data

    def history(self, source, station, limit=72):
        res = (self.sb.table("air_readings").select("data_time,pm10,pm25")
               .eq("source", source).eq("station", station)
               .order("data_time", desc=True).limit(limit).execute())
        return list(reversed(res.data))

    def logs(self, limit=20):
        res = (self.sb.table("collect_logs").select("*")
               .order("ran_at", desc=True).limit(limit).execute())
        return res.data


class MemoryStore:
    kind = "memory(임시)"

    def __init__(self):
        self.rows = {}
        self._logs = []

    def save_readings(self, rows):
        for r in rows:
            self.rows[(r["source"], r["station"], r["data_time"])] = r
        return len(rows)

    def save_log(self, log):
        self._logs.append(log)

    def recent(self, source, limit=300):
        rs = [r for r in self.rows.values() if r["source"] == source]
        return sorted(rs, key=lambda r: r["data_time"], reverse=True)[:limit]

    def history(self, source, station, limit=72):
        rs = [r for r in self.rows.values() if r["source"] == source and r["station"] == station]
        return sorted(rs, key=lambda r: r["data_time"])[-limit:]

    def logs(self, limit=20):
        return list(reversed(self._logs))[:limit]


def get_store():
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if url and key:
        return SupabaseStore(url, key)
    return MemoryStore()
