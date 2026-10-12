from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Cookie, Response, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
import pymysql
import json
import os
import mimetypes
import uuid
import re
import io
import asyncio
import secrets
import hashlib
import datetime

try:
    import openpyxl  # ใช้อ่านไฟล์ Excel (.xlsx) สำหรับ bulk import นัด X-Ray — ถ้าเครื่องนี้ไม่มีเน็ตอาจยังไม่ได้ติดตั้ง
except ImportError:
    openpyxl = None

# ==========================================================
# 📄 โหลดค่าตั้งค่าจากไฟล์ .env (เขียน parser เองแบบง่าย ไม่ใช้ library ภายนอก
# เพราะเครื่อง Server นี้ไม่มีอินเทอร์เน็ต ติดตั้งแพ็กเกจใหม่ไม่ได้)
# ==========================================================
def _load_env_file(filepath=".env"):
    env_vars = {}
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip('"').strip("'")
    return env_vars

_env = _load_env_file()

def get_env(key, default=""):
    """อ่านค่าจาก .env ก่อน ถ้าไม่มีค่อย fallback ไป environment variable ของระบบ แล้วค่อย default"""
    return _env.get(key) or os.environ.get(key) or default

DB_HOST = get_env("DB_HOST", "localhost")
DB_USER = get_env("DB_USER", "root")
DB_PASSWORD = get_env("DB_PASSWORD", "")
DB_NAME = get_env("DB_NAME", "hospital_queue_db")
SERVER_IP = get_env("SERVER_IP", "10.121.111.233")
SECRET_KEY = get_env("SECRET_KEY", "")

if not DB_PASSWORD or not SECRET_KEY:
    raise SystemExit(
        "❌ ไม่พบไฟล์ .env หรือค่าที่จำเป็นยังไม่ครบ (ต้องมี DB_PASSWORD และ SECRET_KEY)\n"
        "   สร้างไฟล์ .env ไว้คู่กับ main.py (ดูตัวอย่างจากไฟล์ .env.example ที่ให้ไปด้วย)"
    )

def normalize_cn(raw: str) -> str:
    """
    ทำให้เลข CN ที่ยิงมาจาก Barcode Scanner กลายเป็นรูปแบบเดียวกันเสมอ
    ไม่สนใจว่าจะมี '-' หรือไม่มี และไม่สนใจตัวพิมพ์เล็ก/ใหญ่
    เช่น '15c15103215' และ '15c15-103215' -> '15C15103215'
    """
    if not raw:
        return ""
    return re.sub(r'[^A-Za-z0-9]', '', raw).upper()

def format_cn(normalized: str) -> str:
    """แปลง CN แบบไม่มีขีดกลับเป็นรูปแบบที่มีขีด เช่น '15C15103215' -> '15C15-103215'"""
    if normalized and len(normalized) == 11:
        return f"{normalized[:5]}-{normalized[5:]}"
    return normalized

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# ==========================================================
# ⚙️ ระบบตั้งค่าที่แก้ได้สด (ไม่ต้อง restart main.py) — เก็บในตาราง app_settings
# ค่าเริ่มต้นด้านล่างนี้ใช้แค่ตอนยังไม่เคยตั้งค่าอะไรในฐานข้อมูลเลย (ครั้งแรกที่รันระบบ)
# ต้องมาก่อนโครงสร้างจุดตรวจด้านล่าง เพราะจำนวนห้อง/เครื่องของแต่ละจุดตอนนี้ดึงมาจากตรงนี้แล้ว
# ==========================================================
DEFAULT_SETTINGS = {
    "avg_Blood": 3, "avg_Eye": 10, "avg_Ear": 10, "avg_Lung": 10, "avg_EKG": 5,
    "avg_XRay": 5, "avg_Ultrasound": 20, "avg_Mammogram": 15, "avg_OBGYN": 20, "avg_Doctor": 10,
    "overdue_multiplier": 2,
    "noshow_threshold_minutes": 45,
    # 🌟 จำนวนห้อง/เครื่องของแต่ละจุดที่มีหลายห้องคู่ขนาน — ปรับได้จากหน้าตั้งค่าระบบ ไม่ต้องแก้โค้ด
    "rooms_Blood": 4, "rooms_Eye": 4, "rooms_Ear": 3, "rooms_EKG": 2, "rooms_Lung": 2, "rooms_Doctor": 7,
    "rooms_XRay": 1, "rooms_Ultrasound": 1, "rooms_Mammogram": 1,
}
SETTINGS = dict(DEFAULT_SETTINGS)  # ใช้ค่า default ไปพลางๆ ก่อนจนกว่าจะโหลดจาก DB จริงตอน startup

def load_settings_from_db():
    """โหลดค่าตั้งค่าทั้งหมดจากตาราง app_settings เข้า SETTINGS (เรียกตอน startup และหลังบันทึกค่าใหม่)"""
    global SETTINGS
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT setting_key, setting_value FROM app_settings")
            rows = cursor.fetchall()
            db_values = {r['setting_key']: r['setting_value'] for r in rows}
    finally:
        connection.close()

    merged = dict(DEFAULT_SETTINGS)
    for key, default_val in DEFAULT_SETTINGS.items():
        if key in db_values:
            try:
                merged[key] = type(default_val)(db_values[key])
            except (ValueError, TypeError):
                pass  # ถ้าค่าที่เก็บไว้แปลงไม่ได้ ใช้ค่า default แทน กันระบบพังจากค่าตั้งค่าเพี้ยน
    SETTINGS = merged

def get_overdue_multiplier() -> float:
    return SETTINGS.get("overdue_multiplier", 2)

def get_noshow_threshold() -> int:
    return int(SETTINGS.get("noshow_threshold_minutes", 45))

def get_room_count(station: str) -> int:
    return int(SETTINGS.get(f"rooms_{station}", 1))

# ==========================================================
# 🏗️ โครงสร้างจุดตรวจ/ห้องย่อย — สร้างใหม่ทุกครั้งที่มีการแก้จำนวนห้อง (ผ่าน rebuild_station_structures())
# ตัวแปรด้านล่างนี้เป็น global ที่ถูก "เขียนทับ" ใหม่ได้ตอน settings เปลี่ยน ไม่ใช่ค่าคงที่ตายตัวอีกต่อไป
# ==========================================================
DOCTOR_ROOM_NUMBERS = []
DOCTOR_STATIONS = []
MULTI_ROOM_STATIONS = {}
ALL_MULTI_ROOM_SUBSTATIONS = []
STATIONS_LIST = []
STATION_LABELS = {
    'Blood': 'ห้องเจาะเลือด',
    'Eye': 'ห้องตรวจตา',
    'Ear': 'ห้องตรวจหู',
    'Lung': 'ห้องตรวจปอด',
    'EKG': 'ห้อง EKG',
    'XRay': 'ห้องเอกซเรย์ (X-Ray ปอด)',
    'Ultrasound': 'ห้องอัลตราซาวด์ (Ultrasound)',
    'Mammogram': 'ห้องแมมโมแกรม (Mammogram)',
    'OBGYN': 'ห้องตรวจสูติ-นรีเวช (OB-GYN)',
    'Doctor': 'ห้องตรวจแพทย์',
}

def rebuild_station_structures():
    """
    สร้าง/อัปเดตโครงสร้างห้องย่อยทั้งหมดใหม่ จากจำนวนห้องปัจจุบันใน SETTINGS
    เรียกครั้งแรกตอน import โมดูล (ใช้ค่า default) แล้วเรียกซ้ำอีกครั้งหลังโหลด/บันทึก settings จริงจาก DB
    """
    global DOCTOR_ROOM_NUMBERS, DOCTOR_STATIONS, MULTI_ROOM_STATIONS, ALL_MULTI_ROOM_SUBSTATIONS, STATIONS_LIST, STATION_LABELS

    # ห้องหมอ: เว้นเลข 7 เสมอ (แยกเป็นห้องสูตินรีเวชแล้ว) นับต่อจากนั้นตามจำนวนที่ตั้งไว้
    doctor_count = get_room_count("Doctor")
    numbers = []
    n = 1
    while len(numbers) < doctor_count:
        if n != 7:
            numbers.append(n)
        n += 1
    DOCTOR_ROOM_NUMBERS = numbers
    DOCTOR_STATIONS = [f"Doctor_{i}" for i in DOCTOR_ROOM_NUMBERS]

    # จุดที่มีหลายห้องคู่ขนานอื่นๆ — เลขห้องต่อเนื่อง 1..N ตามจำนวนที่ตั้งไว้
    new_multi_room_stations = {}
    for station in ['Blood', 'Eye', 'Ear', 'EKG', 'Lung', 'XRay', 'Ultrasound', 'Mammogram']:
        count = get_room_count(station)
        new_multi_room_stations[station] = [f"{station}_{i}" for i in range(1, count + 1)]
    new_multi_room_stations["Doctor"] = DOCTOR_STATIONS
    MULTI_ROOM_STATIONS = new_multi_room_stations

    ALL_MULTI_ROOM_SUBSTATIONS = [s for sub_list in MULTI_ROOM_STATIONS.values() for s in sub_list]
    STATIONS_LIST = ['Blood', 'Eye', 'Ear', 'Lung', 'EKG', 'XRay', 'Ultrasound', 'Mammogram', 'OBGYN', 'Doctor'] + ALL_MULTI_ROOM_SUBSTATIONS

    # ป้ายกำกับห้องย่อย (เช่น "ห้องเจาะเลือด ช่อง 2") — สร้างใหม่ทุกครั้งกันเลขห้องเก่าค้าง
    for station, sub_list in MULTI_ROOM_STATIONS.items():
        for idx, sub in enumerate(sub_list, start=1):
            STATION_LABELS[sub] = f"{STATION_LABELS[station]} ช่อง {idx}"

rebuild_station_structures()  # เรียกครั้งแรกตอน import โมดูล ใช้ค่า default ไปพลางๆ ก่อน

# สถานีตรวจ "ทั่วไป" ที่ต้องรอการกำหนดโปรแกรมตรวจ (Program Assignment) จากพยาบาลที่หน้า nurse_manage.html ก่อน
# ถึงจะถูกปลดล็อกเป็น Lock (รอสแกน CN เพื่อเข้าคิวจริง) — ป้องกันการเดินข้ามขั้นตอนไปสุ่มทำจุดใดจุดหนึ่งก่อนได้ตามใจชอบ
# 🌟 แยก X-Ray เป็น 3 ประเภทตรวจ (XRay ปอด / Ultrasound / Mammogram) เพราะเป็นการตรวจคนละแบบ คนละคิวกัน
# 🌟 แยกห้องสูติ-นรีเวช (OBGYN) ออกจากกลุ่มห้องหมอทั่วไป — เดิมคือห้อง 7 ในกลุ่มหมอ ตอนนี้เป็นจุดตรวจของตัวเอง
# ✅ ยกเลิกจุด VitalSign ออกจากระบบคิวแล้ว — กำหนดโปรแกรมตรวจตรงที่ nurse_manage.html แทนหลังลงทะเบียนเลย
GENERAL_TEST_STATIONS = ['Blood', 'Eye', 'Ear', 'Lung', 'EKG', 'XRay', 'Ultrasound', 'Mammogram', 'OBGYN']

STATION_MAP = {
    'เจาะเลือด': 'Blood',
    'ตรวจตา': 'Eye',
    'ตรวจหู': 'Ear',
    'ตรวจปอด': 'Lung',
    'ห้องตรวจคลื่นไฟฟ้าหัวใจ': 'EKG',
    'คลื่นไฟฟ้าหัวใจ': 'EKG',
    'เอกซเรย์': 'XRay',
    'X-Ray': 'XRay',
    'อัลตราซาวด์': 'Ultrasound',
    'แมมโมแกรม': 'Mammogram',
    'สูติ': 'OBGYN',
    'ห้องตรวจสูติ': 'OBGYN',
    'ห้องตรวจแพทย์': 'Doctor'
}

def normalize_station_name(name: str) -> str:
    if not name:
        return ""
    clean_name = name.strip()
    return STATION_MAP.get(clean_name, clean_name)

def get_avg_minutes(station: str) -> float:
    return SETTINGS.get(f"avg_{get_canonical_station(station)}", 5)

# ==========================================================
# 🚫 ปิดห้อง/เครื่องเฉพาะชั่วคราว (เช่น ห้องตามี 4 เครื่อง ปิดเครื่อง 2 เพราะพยาบาลลาป่วย)
# ==========================================================
CLOSED_ROOMS = set()  # เก็บเป็น set ของ "Eye_2", "Blood_3" ฯลฯ — โหลดจาก DB ตอน startup

def load_closed_rooms_from_db():
    global CLOSED_ROOMS
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT room_name FROM closed_rooms")
            CLOSED_ROOMS = {r['room_name'] for r in cursor.fetchall()}
    finally:
        connection.close()

def get_canonical_station(station_name: str) -> str:
    """ใช้หาว่า station_name (เช่น 'Doctor_3' หรือ 'Blood_2') ควรอิงเวลาเฉลี่ย/การจัดกลุ่มของจุดไหน (เช่น 'Doctor', 'Blood')"""
    if station_name.lower().startswith("doctor_"):
        return "Doctor"
    for _station, _sub_list in MULTI_ROOM_STATIONS.items():
        if station_name in _sub_list:
            return _station
    return station_name

def is_multi_room_substation(station_name: str) -> bool:
    """เช็คว่า station_name นี้เป็นห้องย่อยของจุดที่มีหลายห้องคู่ขนานหรือไม่ (Doctor_N หรือ Blood_N/Eye_N/Ear_N/EKG_N/Lung_N)"""
    if station_name.lower().startswith("doctor_"):
        return True
    return station_name in ALL_MULTI_ROOM_SUBSTATIONS

def fetch_queue_data():
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # Auto-Hold หากเกิน 15 นาที
            cursor.execute("""
                UPDATE queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                SET qs.status = 'Hold'
                WHERE qs.status IN ('Calling', 'Pending') 
                  AND TIMESTAMPDIFF(MINUTE, qs.updated_at, NOW()) >= 15
                  AND DATE(q.created_at) = CURDATE()
            """)

            cursor.execute("""
                SELECT qs.station_name, q.queue_number, q.queue_type, q.patient_name
                FROM queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                WHERE qs.status = 'Calling' AND DATE(q.created_at) = CURDATE()
            """)
            rows = cursor.fetchall()
            current_calls = {row['station_name']: row['queue_number'] for row in rows}
            # 🌟 ชื่อคนไข้ที่กำลังถูกเรียกอยู่ ณ จุดนั้นๆ — ใช้ประกาศชื่อคู่กับเลขคิวบนจอ TV
            current_calls_names = {row['station_name']: row['patient_name'] for row in rows}

            for st in STATIONS_LIST:
                if st not in current_calls:
                    current_calls[st] = "-"

            calling_map = {qn: st for st, qn in current_calls.items() if qn != "-"}

            waiting_by_station = {}
            history_by_station = {}

            for st in STATIONS_LIST:
                cursor.execute("""
                    SELECT 
                        q.queue_number, 
                        q.patient_name as name, 
                        q.queue_type,
                        qs.status,
                        TIMESTAMPDIFF(MINUTE, qs.updated_at, NOW()) as wait_minutes
                    FROM queue_stations qs
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    WHERE LOWER(qs.station_name) = LOWER(%s) 
                      AND qs.status IN ('Pending', 'Hold') 
                      AND DATE(q.created_at) = CURDATE()
                    GROUP BY q.queue_number, q.patient_name, q.queue_type, qs.status, qs.updated_at, q.priority_time, q.created_at
                    ORDER BY 
                        CASE WHEN qs.status = 'Hold' THEN 0 ELSE 1 END ASC,
                        DATE_SUB(qs.updated_at, INTERVAL TIMESTAMPDIFF(SECOND, q.priority_time, q.created_at) SECOND) ASC
                """, (st,))
                rows = cursor.fetchall()

                for r in rows:
                    busy_station = calling_map.get(r['queue_number'])
                    if busy_station and busy_station.lower() != st.lower():
                        r['busy_at'] = busy_station
                        r['busy_label'] = STATION_LABELS.get(busy_station, busy_station)
                    else:
                        r['busy_at'] = None
                        r['busy_label'] = None

                waiting_by_station[st] = rows

                # 🌟 ประวัติ "ตรวจเสร็จแล้ว" ต้องรวมจากทุกห้องย่อยด้วย ถ้าเป็นจุดที่มีหลายห้องคู่ขนาน
                # เพราะตอนเรียกคิว แถวจะถูกเปลี่ยนชื่อจาก canonical (เช่น "Blood") เป็นห้องเฉพาะ (เช่น "Blood_2")
                # ถ้า query แค่ชื่อ canonical เฉยๆ จะไม่เจอประวัติเลย เพราะไม่มีแถวไหนชื่อ "Blood" ตรงๆ หลังตรวจเสร็จ
                if st in MULTI_ROOM_STATIONS:
                    history_station_names = MULTI_ROOM_STATIONS[st]
                elif st == "Doctor":
                    history_station_names = DOCTOR_STATIONS
                else:
                    history_station_names = [st]

                cursor.execute("""
                    SELECT q.queue_number, q.patient_name as name, q.queue_type, DATE_FORMAT(MAX(qs.updated_at), '%%H:%%i:%%s') as updated_at
                    FROM queue_stations qs
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    WHERE qs.station_name IN %s AND qs.status = 'Completed' AND DATE(q.created_at) = CURDATE()
                    GROUP BY q.queue_number, q.patient_name, q.queue_type
                    ORDER BY MAX(qs.updated_at) DESC LIMIT 10
                """, (tuple(history_station_names),))
                history_by_station[st] = cursor.fetchall()

            return {
                "current_calls": current_calls,
                "current_calls_names": current_calls_names,
                "waiting_patients": waiting_by_station,
                "history_patients": history_by_station,
                "station_labels": STATION_LABELS,
                "closed_rooms": list(CLOSED_ROOMS),
                "multi_room_stations": MULTI_ROOM_STATIONS  # เช่น {"Blood": ["Blood_1","Blood_2","Blood_3","Blood_4"], ...}
                                                              # ให้หน้าเรียกคิวสร้างปุ่มเลือกห้องตามจำนวนจริงที่ตั้งไว้ ไม่ใช่ปุ่มตายตัว
            }
    finally:
        connection.close()

@app.on_event("startup")
async def start_periodic_broadcast():
    """
    ยิงข้อมูลสถานะคิวออกไปให้ทุกหน้าจอที่เปิดอยู่ทุก 60 วินาที แม้ไม่มีใครกดอะไรเลย
    จำเป็นเพราะข้อมูล "รอมากี่นาทีแล้ว" (wait_minutes) ต้องอัปเดตตามเวลาจริง
    ถ้าไม่มี broadcast เป็นระยะ หน้าจอจะไม่รู้ว่าเวลาผ่านไปแล้วจนกว่าจะมีคนกดปุ่มอื่นในระบบ
    ซึ่งจำเป็นสำหรับ popup แจ้งเตือน "รอเกิน 15 นาที" ที่หน้าจอเรียกคิวแต่ละจุด
    """
    async def periodic_loop():
        while True:
            await asyncio.sleep(60)
            try:
                data = fetch_queue_data()
                await manager.broadcast(json.dumps(data))
            except Exception as e:
                print(f"⚠️ [periodic broadcast error] {type(e).__name__}: {e}")

    asyncio.create_task(periodic_loop())

@app.on_event("startup")
async def load_dynamic_config():
    """โหลดค่าตั้งค่าระบบ + สถานะห้องที่ปิดไว้ จาก DB เข้าหน่วยความจำตอนเริ่มโปรแกรม"""
    try:
        load_settings_from_db()
        rebuild_station_structures()  # สร้างโครงสร้างห้องย่อยใหม่ตามจำนวนห้องจริงที่ตั้งไว้ใน DB (ไม่ใช่แค่ค่า default อีกต่อไป)
        print(f"⚙️  โหลดค่าตั้งค่าระบบสำเร็จ: {SETTINGS}")
    except Exception as e:
        print(f"⚠️ โหลดค่าตั้งค่าระบบไม่สำเร็จ ({type(e).__name__}: {e}) — ใช้ค่า default แทน")

    try:
        load_closed_rooms_from_db()
        print(f"🚪 โหลดสถานะห้องที่ปิดไว้สำเร็จ: {CLOSED_ROOMS}")
    except Exception as e:
        print(f"⚠️ โหลดสถานะห้องที่ปิดไว้ไม่สำเร็จ ({type(e).__name__}: {e})")

@app.websocket("/ws/queue")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        initial_data = fetch_queue_data()
        await websocket.send_text(json.dumps(initial_data))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

class RegisterPatientModel(BaseModel):
    patient_name: str
    queue_type: str = "walkin"
    cn: Optional[str] = None
    hn: Optional[str] = None
    birth_date: Optional[str] = None
    gender: Optional[str] = None

class PatientAddModel(BaseModel):
    """สำหรับเพิ่มผู้ป่วยใหม่เมื่อ CN ไม่เจอในระบบ"""
    cn: str
    first_name: str
    last_name: str
    gender: Optional[str] = None
    birth_date: Optional[str] = None  # รูปแบบ dd/mm/yyyy
    hn: Optional[str] = None

class CallQueueModel(BaseModel):
    queue_number: str
    station_name: str

class AssignProgramModel(BaseModel):
    queue_number: str
    stations: List[str]   # รายชื่อจุดตรวจทั่วไปที่ยืนยันว่าคนไข้ต้องตรวจจริง (ตัดสินใจหลัง VitalSign)
    include_doctor: bool = True   # ต้องพบแพทย์หรือไม่ (เผื่อบาง Case ตรวจเสร็จจบที่การเงินเลย)

# ==========================================================
# 🔐 ระบบ Login พื้นฐาน — ป้องกันไม่ให้ใครก็เปิดหน้าเรียกคิว/แก้ข้อมูลได้โดยไม่ยืนยันตัวตน
# ==========================================================

class LoginModel(BaseModel):
    username: str
    password: str

# เก็บ session ไว้ในหน่วยความจำ (RAM) — ถ้า restart main.py ทุกคนต้อง login ใหม่หมด
# (ยอมรับข้อจำกัดนี้เพื่อความง่าย ไม่ต้องเพิ่มตาราง/ระบบซับซ้อนสำหรับระบบขนาดนี้)
SESSIONS = {}
SESSION_LIFETIME_HOURS = 12

def hash_password(password: str, salt: bytes = None) -> str:
    """แปลงรหัสผ่านเป็น hash ที่ปลอดภัย (PBKDF2) — ใช้แค่ hashlib ที่มีอยู่ใน Python อยู่แล้ว ไม่ต้องติดตั้งอะไรเพิ่ม"""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', (password + SECRET_KEY).encode('utf-8'), salt, 100000)
    return salt.hex() + ':' + dk.hex()

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, hash_hex = stored_hash.split(':')
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac('sha256', (password + SECRET_KEY).encode('utf-8'), salt, 100000)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False

def get_current_user(session_token: Optional[str]) -> Optional[dict]:
    if not session_token or session_token not in SESSIONS:
        return None
    session = SESSIONS[session_token]
    if session['expires_at'] < datetime.datetime.now():
        del SESSIONS[session_token]
        return None
    return session

async def require_auth(session_token: Optional[str] = Cookie(None)) -> dict:
    """Dependency สำหรับ endpoint ที่ต้อง login ก่อนถึงจะใช้ได้ (ใส่ Depends(require_auth) ในพารามิเตอร์)"""
    user = get_current_user(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบก่อนใช้งาน")
    return user

async def require_admin(session_token: Optional[str] = Cookie(None)) -> dict:
    """Dependency สำหรับ endpoint ที่ต้องเป็น admin เท่านั้น (เช่น หน้าตั้งค่าระบบ, ดู audit log)"""
    user = get_current_user(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบก่อนใช้งาน")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="ต้องเป็นผู้ดูแลระบบ (admin) เท่านั้นถึงจะใช้งานหน้านี้ได้")
    return user

def log_audit(username: str, action: str, detail: str = ""):
    """บันทึกว่าใครทำอะไรไปบ้าง (audit trail) — ไม่ทำให้ request ล้มเหลวถ้าบันทึกไม่สำเร็จ"""
    try:
        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO audit_log (username, action, detail) VALUES (%s, %s, %s)",
                    (username, action, detail)
                )
        finally:
            connection.close()
    except Exception as e:
        print(f"⚠️ [audit log error] {type(e).__name__}: {e}")

@app.post("/api/login")
async def login(payload: LoginModel, response: Response):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE username = %s AND is_active = 1",
                (payload.username,)
            )
            user = cursor.fetchone()
    finally:
        connection.close()

    if not user or not verify_password(payload.password, user['password_hash']):
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

    token = secrets.token_hex(32)
    SESSIONS[token] = {
        "username": user['username'],
        "full_name": user['full_name'],
        "role": user['role'],
        "expires_at": datetime.datetime.now() + datetime.timedelta(hours=SESSION_LIFETIME_HOURS)
    }
    response.set_cookie(key="session_token", value=token, httponly=True, max_age=SESSION_LIFETIME_HOURS * 3600, samesite="lax")

    log_audit(user['username'], "login", "")

    return {"status": "success", "username": user['username'], "full_name": user['full_name'], "role": user['role']}

@app.post("/api/logout")
async def logout(response: Response, session_token: Optional[str] = Cookie(None)):
    if session_token and session_token in SESSIONS:
        log_audit(SESSIONS[session_token]['username'], "logout", "")
        del SESSIONS[session_token]
    response.delete_cookie("session_token")
    return {"status": "success"}

@app.get("/api/auth/check")
async def auth_check(session_token: Optional[str] = Cookie(None)):
    user = get_current_user(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="ยังไม่ได้เข้าสู่ระบบ")
    return {"status": "success", "username": user['username'], "full_name": user['full_name'], "role": user['role']}

@app.get("/api/health")
def read_root():
    return {"status": "running"}

@app.get("/api/stats")
def get_stats(date: Optional[str] = None):
    """
    สรุปยอดผู้ใช้บริการรายวัน — สำหรับหน้า Dashboard สถิติ
    ใช้ query param ?date=YYYY-MM-DD เพื่อดูวันอื่นย้อนหลังได้ (ไม่ใส่ = วันนี้)
    """
    import datetime as _dt
    target_date = date or _dt.date.today().isoformat()

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # 1) จำนวนคนไข้รวมวันนั้น + แยกตามประเภท (Walk-in / นัดหมาย / Mobile)
            cursor.execute(
                "SELECT queue_type, COUNT(*) as cnt FROM hospital_queues WHERE DATE(created_at) = %s AND is_cancelled = 0 GROUP BY queue_type",
                (target_date,)
            )
            type_rows = cursor.fetchall()
            total_patients = sum(r['cnt'] for r in type_rows)
            walkin_count = next((r['cnt'] for r in type_rows if r['queue_type'] == 'walkin'), 0)
            appointment_count = next((r['cnt'] for r in type_rows if r['queue_type'] == 'appointment'), 0)
            mobile_count = next((r['cnt'] for r in type_rows if r['queue_type'] == 'mobile'), 0)

            # 2) จำนวนที่แต่ละจุดให้บริการ "เสร็จแล้ว" (Completed) วันนั้น
            # 🐞 แก้บั๊กเดียวกับที่เจอตอนประวัติตรวจเสร็จ/track.html — จุดที่มีหลายห้องคู่ขนาน (Blood/Eye/Ear/EKG/Lung)
            # พอถูกเรียกคิว ชื่อแถวจะถูกเปลี่ยนจาก canonical เป็นห้องเฉพาะ (เช่น Blood -> Blood_2) แล้วค้างชื่อนั้นไว้ตอน Completed
            # ต้องนับรวมจากทุกห้องย่อยด้วย ไม่ใช่แค่ชื่อ canonical ตรงๆ
            by_station = []
            for st in GENERAL_TEST_STATIONS:
                station_names_to_check = MULTI_ROOM_STATIONS.get(st, [st])
                cursor.execute(
                    """SELECT COUNT(*) as cnt 
                       FROM queue_stations qs
                       JOIN hospital_queues q ON qs.queue_id = q.id
                       WHERE qs.station_name IN %s AND qs.status = 'Completed' AND DATE(q.created_at) = %s""",
                    (tuple(station_names_to_check), target_date)
                )
                cnt = cursor.fetchone()['cnt']
                by_station.append({"station": st, "label": STATION_LABELS.get(st, st), "completed": cnt})

            # รวมห้องหมอทุกห้อง (Doctor, Doctor_1..8) เป็นยอดเดียว "พบแพทย์"
            cursor.execute(
                """SELECT COUNT(*) as cnt
                   FROM queue_stations qs
                   JOIN hospital_queues q ON qs.queue_id = q.id
                   WHERE (LOWER(qs.station_name) = 'doctor' OR LOWER(qs.station_name) LIKE 'doctor_%%')
                     AND qs.status = 'Completed' AND DATE(q.created_at) = %s""",
                (target_date,)
            )
            doctor_cnt = cursor.fetchone()['cnt']
            by_station.append({"station": "Doctor", "label": "พบแพทย์", "completed": doctor_cnt})

            # 3) จำนวนคนไข้ลงทะเบียนแยกตามชั่วโมง (สำหรับกราฟแนวโน้มตลอดวัน)
            cursor.execute(
                """SELECT HOUR(created_at) as hr, COUNT(*) as cnt
                   FROM hospital_queues
                   WHERE DATE(created_at) = %s
                   GROUP BY HOUR(created_at)
                   ORDER BY hr""",
                (target_date,)
            )
            hourly_rows = cursor.fetchall()
            hourly = {str(h).zfill(2): 0 for h in range(24)}
            for r in hourly_rows:
                hourly[str(r['hr']).zfill(2)] = r['cnt']

            return {
                "status": "success",
                "date": target_date,
                "total_patients": total_patients,
                "walkin_count": walkin_count,
                "appointment_count": appointment_count,
                "mobile_count": mobile_count,
                "by_station": by_station,
                "hourly": hourly
            }
    finally:
        connection.close()

@app.get("/api/overdue-alerts")
def get_overdue_alerts():
    """
    🚨 รายชื่อคนไข้ที่ "รอเกินเวลาที่กำหนด" ที่จุดใดจุดหนึ่งอยู่ตอนนี้ (วันนี้เท่านั้น)
    เกณฑ์: ค้างอยู่ในสถานะ Pending/Hold/Calling ที่จุดนั้นนานกว่า (เวลาเฉลี่ยของจุดนั้น x OVERDUE_MULTIPLIER)
    ใช้แสดงในหน้าจัดการคิว เพื่อให้เจ้าหน้าที่เห็นและตามคนไข้ที่ค้างนานผิดปกติได้ทันที
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT q.queue_number, q.patient_name, qs.station_name, qs.status,
                       TIMESTAMPDIFF(MINUTE, qs.updated_at, NOW()) as elapsed_minutes
                FROM queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                WHERE qs.status IN ('Pending', 'Hold', 'Calling')
                  AND DATE(q.created_at) = CURDATE()
                ORDER BY elapsed_minutes DESC
            """)
            rows = cursor.fetchall()

        overdue_list = []
        for r in rows:
            avg_minutes = get_avg_minutes(r['station_name'])
            threshold = avg_minutes * get_overdue_multiplier()

            if r['elapsed_minutes'] > threshold:
                overdue_list.append({
                    "queue_number": r['queue_number'],
                    "patient_name": r['patient_name'],
                    "station": r['station_name'],
                    "label": STATION_LABELS.get(r['station_name'], r['station_name']),
                    "status": r['status'],
                    "elapsed_minutes": r['elapsed_minutes'],
                    "threshold_minutes": threshold
                })

        return {
            "status": "success",
            "count": len(overdue_list),
            "overdue": overdue_list
        }
    finally:
        connection.close()

@app.get("/api/noshow-candidates")
def get_noshow_candidates():
    """
    🚫 รายชื่อคิวที่ถูกพักไว้ (Hold) นานเกิน NOSHOW_HOLD_THRESHOLD_MINUTES โดยไม่มีการเคลื่อนไหวเลย
    ถือเป็น "ผู้สมัครใจที่อาจไม่มาแล้ว" ให้พนักงานพิจารณา — ไม่ปิดคิวให้อัตโนมัติ ต้องให้คนกดยืนยันเอง
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT q.queue_number, q.patient_name, qs.station_name,
                       TIMESTAMPDIFF(MINUTE, qs.updated_at, NOW()) as held_minutes
                FROM queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                WHERE qs.status = 'Hold'
                  AND DATE(q.created_at) = CURDATE()
                  AND TIMESTAMPDIFF(MINUTE, qs.updated_at, NOW()) >= %s
                ORDER BY held_minutes DESC
            """, (get_noshow_threshold(),))
            rows = cursor.fetchall()

        candidates = [{
            "queue_number": r['queue_number'],
            "patient_name": r['patient_name'],
            "station": r['station_name'],
            "label": STATION_LABELS.get(r['station_name'], r['station_name']),
            "held_minutes": r['held_minutes']
        } for r in rows]

        return {"status": "success", "count": len(candidates), "candidates": candidates}
    finally:
        connection.close()

class NoShowModel(BaseModel):
    queue_number: str
    station_name: str

@app.post("/api/mark-noshow")
async def mark_noshow(payload: NoShowModel, current_user: dict = Depends(require_auth)):
    """
    🚫 ทำเครื่องหมายว่าคนไข้ "ไม่มาตามนัด" ที่จุดนี้ — ปิดจุดนี้เป็น Skipped
    ต้องให้พนักงาน login แล้วกดยืนยันเองเท่านั้น (ระบบไม่ปิดให้อัตโนมัติ)
    """
    target_station = normalize_station_name(payload.station_name)
    log_audit(current_user['username'], "mark-noshow", f"{payload.queue_number} @ {target_station}")

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                SET qs.status = 'Skipped'
                WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) AND DATE(q.created_at) = CURDATE()
            """, (payload.queue_number, target_station))

            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="ไม่พบคิวหรือจุดบริการนี้")

            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.get("/api/stats-range")
def get_stats_range(start_date: str, end_date: str):
    """
    📊 เปรียบเทียบสถิติหลายวัน — คืนยอดคนไข้รวม/แยกประเภท ของแต่ละวันในช่วงที่กำหนด
    ใช้วาดกราฟเทียบแนวโน้มหลายวัน/หลายสัปดาห์/หลายเดือนในหน้า Dashboard
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT DATE(created_at) as day, queue_type, COUNT(*) as cnt
                FROM hospital_queues
                WHERE DATE(created_at) BETWEEN %s AND %s AND is_cancelled = 0
                GROUP BY DATE(created_at), queue_type
                ORDER BY day ASC
            """, (start_date, end_date))
            rows = cursor.fetchall()

        by_day = {}
        for r in rows:
            day_str = r['day'].isoformat()
            if day_str not in by_day:
                by_day[day_str] = {"date": day_str, "total": 0, "walkin": 0, "appointment": 0, "mobile": 0}
            by_day[day_str]["total"] += r['cnt']
            if r['queue_type'] == 'walkin':
                by_day[day_str]["walkin"] += r['cnt']
            elif r['queue_type'] == 'appointment':
                by_day[day_str]["appointment"] += r['cnt']
            elif r['queue_type'] == 'mobile':
                by_day[day_str]["mobile"] += r['cnt']

        return {
            "status": "success",
            "start_date": start_date,
            "end_date": end_date,
            "days": list(by_day.values())
        }
    finally:
        connection.close()

# ==========================================================
# ⚙️ หน้าตั้งค่าระบบ (Settings) — เฉพาะ admin เท่านั้น
# ==========================================================

@app.get("/api/settings")
async def get_settings(current_user: dict = Depends(require_admin)):
    return {"status": "success", "settings": SETTINGS}

class SettingsUpdateModel(BaseModel):
    settings: dict

@app.post("/api/settings")
async def update_settings(payload: SettingsUpdateModel, current_user: dict = Depends(require_admin)):
    """
    บันทึกค่าตั้งค่าใหม่ลง DB แล้วโหลดเข้าหน่วยความจำทันที — มีผลทันทีโดยไม่ต้อง restart main.py
    รับเฉพาะ key ที่รู้จัก (อยู่ใน DEFAULT_SETTINGS) เท่านั้น กัน key แปลกปลอมหลุดเข้าไป
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            for key, value in payload.settings.items():
                if key not in DEFAULT_SETTINGS:
                    continue  # ข้าม key ที่ไม่รู้จักไปเงียบๆ กันข้อมูลแปลกปลอม
                cursor.execute("""
                    INSERT INTO app_settings (setting_key, setting_value)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                """, (key, str(value)))
            connection.commit()
    finally:
        connection.close()

    load_settings_from_db()
    rebuild_station_structures()  # เผื่อมีการแก้จำนวนห้อง/เครื่อง ต้องสร้างโครงสร้างห้องย่อยใหม่ทันที
    log_audit(current_user['username'], "update-settings", json.dumps(payload.settings, ensure_ascii=False))

    # แจ้งทุกหน้าจอที่เปิดอยู่ (นับห้อง TV Dashboard ฯลฯ) ว่าโครงสร้างห้องเปลี่ยนไปแล้ว ให้รีเฟรชปุ่มเลือกห้องทันที
    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))

    return {"status": "success", "settings": SETTINGS}

# ==========================================================
# 🚪 ปิดห้อง/เครื่องเฉพาะชั่วคราว — เช่น ห้องตามี 4 เครื่อง ปิดเครื่อง 2 เพราะพยาบาลลาป่วยกะทันหัน
# ==========================================================

@app.get("/api/rooms")
async def get_rooms_status(current_user: dict = Depends(require_auth)):
    """รายชื่อห้องย่อยทั้งหมดของทุกจุดที่มีหลายห้อง พร้อมสถานะเปิด/ปิด"""
    rooms_by_station = {}
    for station, room_list in MULTI_ROOM_STATIONS.items():
        rooms_by_station[station] = [
            {"room": r, "is_closed": r in CLOSED_ROOMS} for r in room_list
        ]
    return {"status": "success", "rooms": rooms_by_station}

class ToggleRoomModel(BaseModel):
    room_name: str      # เช่น "Eye_2"
    is_closed: bool
    reason: Optional[str] = ""

@app.post("/api/toggle-room")
async def toggle_room(payload: ToggleRoomModel, current_user: dict = Depends(require_auth)):
    """เปิด/ปิดห้องย่อยเฉพาะ — ห้องที่ปิดจะไม่ให้เรียกคิวเข้าห้องนั้นได้ (พนักงานทุกคน login แล้วทำได้ ไม่ต้อง admin)"""
    room_name = payload.room_name
    canonical = get_canonical_station(room_name)
    if canonical not in MULTI_ROOM_STATIONS or room_name not in MULTI_ROOM_STATIONS[canonical]:
        raise HTTPException(status_code=400, detail="ไม่พบห้องนี้ในระบบ")

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            if payload.is_closed:
                cursor.execute("""
                    INSERT INTO closed_rooms (room_name, reason, closed_by)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE reason = VALUES(reason), closed_by = VALUES(closed_by)
                """, (room_name, payload.reason or "", current_user['username']))
            else:
                cursor.execute("DELETE FROM closed_rooms WHERE room_name = %s", (room_name,))
            connection.commit()
    finally:
        connection.close()

    load_closed_rooms_from_db()
    log_audit(current_user['username'], "toggle-room", f"{room_name} -> {'closed' if payload.is_closed else 'open'} ({payload.reason or ''})")

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success", "closed_rooms": list(CLOSED_ROOMS)}

# ==========================================================
# 🔑 เปลี่ยนรหัสผ่านของตัวเอง
# ==========================================================

class ChangePasswordModel(BaseModel):
    current_password: str
    new_password: str

@app.post("/api/change-password")
async def change_password(payload: ChangePasswordModel, current_user: dict = Depends(require_auth)):
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องมีอย่างน้อย 6 ตัวอักษร")

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT password_hash FROM users WHERE username = %s", (current_user['username'],))
            row = cursor.fetchone()
            if not row or not verify_password(payload.current_password, row['password_hash']):
                raise HTTPException(status_code=401, detail="รหัสผ่านปัจจุบันไม่ถูกต้อง")

            new_hash = hash_password(payload.new_password)
            cursor.execute("UPDATE users SET password_hash = %s WHERE username = %s", (new_hash, current_user['username']))
            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "change-password", "")
    return {"status": "success"}

# ==========================================================
# 📜 ดูประวัติการใช้งาน (Audit Log) — เฉพาะ admin เท่านั้น
# ==========================================================

@app.get("/api/audit-log")
async def get_audit_log(
    date: Optional[str] = None,
    username: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 200,
    current_user: dict = Depends(require_admin)
):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conditions = []
            params = []
            if date:
                conditions.append("DATE(created_at) = %s")
                params.append(date)
            if username:
                conditions.append("username = %s")
                params.append(username)
            if action:
                conditions.append("action = %s")
                params.append(action)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(min(limit, 1000))

            cursor.execute(f"""
                SELECT username, action, detail, DATE_FORMAT(created_at, '%%d/%%m/%%Y %%H:%%i:%%s') as created_at_str
                FROM audit_log
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %s
            """, tuple(params))
            rows = cursor.fetchall()

            cursor.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
            all_actions = [r['action'] for r in cursor.fetchall()]
    finally:
        connection.close()

    return {"status": "success", "logs": rows, "available_actions": all_actions}


# เหตุผลที่ไม่ใช้ edge-tts/Google TTS: เครื่อง Server นี้ไม่มีอินเทอร์เน็ตเลย
# ต้องใช้เสียงที่มีอยู่ในเครื่อง Windows อยู่แล้ว (ออฟไลน์ 100% ไม่ต้องต่อเน็ต)
# สร้างเสียงที่ฝั่ง Server แล้วส่งเป็นไฟล์ WAV ให้ TV เล่นแทน — เพราะ TV ส่วนใหญ่
# ไม่รองรับ Web Speech API ของเบราว์เซอร์เลย แต่การ "เล่นไฟล์เสียง" ทำได้แน่นอนทุกรุ่น
import pyttsx3
import tempfile

# คำที่มักปรากฏในชื่อเสียงผู้หญิง/ผู้ชายของแต่ละ engine (Microsoft SAPI5)
FEMALE_VOICE_HINTS = ['female', 'ผู้หญิง', 'zira', 'premwadee', 'pattara', 'achara', 'narisa', 'kanya']
MALE_VOICE_HINTS = ['male', 'ผู้ชาย', 'david', 'mark', 'niwat']

LANG_NAME_KEYWORDS = {
    "th": ["thai", "th-th", "th_th"],
    "en": ["english", "en-us", "en_us", "en-gb", "en_gb"],
}

def find_best_voice(engine, lang_prefix: str):
    """หาเสียงที่ตรงภาษาที่สุด โดยให้ความสำคัญกับเสียงผู้หญิงเป็นอันดับแรก
    ใช้ชื่อเสียง (voice.name) ในการเทียบภาษา เพราะ SAPI5 บน Windows มักไม่ใส่ค่า languages มาให้"""
    voices = engine.getProperty('voices')
    keywords = LANG_NAME_KEYWORDS.get(lang_prefix, [lang_prefix])

    candidates = [v for v in voices if any(kw in v.name.lower() for kw in keywords)]

    if not candidates:
        return None

    def score(v):
        name_lower = v.name.lower()
        s = 0
        if any(hint in name_lower for hint in FEMALE_VOICE_HINTS):
            s += 10
        if any(hint in name_lower for hint in MALE_VOICE_HINTS):
            s -= 10
        return s

    candidates.sort(key=score, reverse=True)
    return candidates[0]

@app.get("/api/tts-voices")
def list_tts_voices():
    """เช็คว่าเครื่องนี้มีเสียง (SAPI5) อะไรติดตั้งอยู่บ้าง — ใช้ debug ตอนเสียงไม่ตรงภาษา/เพศที่ต้องการ"""
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        result = [{"id": v.id, "name": v.name} for v in voices]
        engine.stop()
        return {"status": "success", "count": len(result), "voices": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"เช็คเสียงไม่สำเร็จ: {str(e)}")

@app.get("/api/tts")
def text_to_speech(text: str, lang: str = "th", speed: float = 0.75):
    """
    สร้างเสียงพูดจากข้อความโดยใช้เสียงในเครื่อง Windows (ออฟไลน์ ไม่ต้องต่อเน็ต)
    คืนเป็นไฟล์ WAV ให้ฝั่ง client (TV Dashboard) เล่นตรงๆ
    ตัวอย่าง: /api/tts?text=ขอเชิญหมายเลขคิว W004&lang=th&speed=0.75

    หมายเหตุ: เป็น sync def (ไม่ใช่ async) ตั้งใจ — FastAPI จะรันใน thread pool ให้อัตโนมัติ
    เพราะ pyttsx3 เป็นไลบรารีแบบ blocking ใช้กับ async ตรงๆ ไม่ได้
    """
    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="ต้องระบุข้อความที่จะพูด (text)")

    lang_prefix = "th" if lang == "th" else "en"
    tmp_path = None

    try:
        engine = pyttsx3.init()

        voice = find_best_voice(engine, lang_prefix)
        if voice:
            engine.setProperty('voice', voice.id)
            print(f"✅ [TTS] ใช้เสียง: {voice.name}")
        else:
            print(f"⚠️ [TTS] ไม่พบเสียงสำหรับภาษา '{lang}' — ใช้เสียง default ของเครื่อง (อาจไม่ตรงภาษา)")

        # ปรับความเร็ว: ค่า rate ปกติของ pyttsx3 อยู่ที่ประมาณ 200 คำ/นาที
        base_rate = 200
        engine.setProperty('rate', int(base_rate * speed))
        engine.setProperty('volume', 1.0)

        # เขียนเสียงลงไฟล์ชั่วคราว แล้วอ่านกลับมาส่งให้ client
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        engine.save_to_file(text, tmp_path)
        engine.runAndWait()
        engine.stop()

        with open(tmp_path, "rb") as f:
            audio_data = f.read()

        if not audio_data:
            raise HTTPException(status_code=500, detail="สร้างเสียงไม่สำเร็จ (ได้ไฟล์เปล่า)")

        return Response(content=audio_data, media_type="audio/wav", headers={"Cache-Control": "no-cache"})

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ [TTS error] {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"สร้างเสียงไม่สำเร็จ: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

@app.get("/api/patient-lookup/{cn_raw}")
async def patient_lookup(cn_raw: str):
    """
    ค้นหาข้อมูลผู้ป่วยจากเลข CN ที่ยิงมาจาก Barcode บนบัตรคิว/บัตรผู้ป่วย
    รับได้ทั้งแบบมีขีด (15C15-103215) และไม่มีขีด (15C15103215)
    """
    try:
        print(f"🔍 [patient_lookup] CN raw: {cn_raw}")
        cn_norm = normalize_cn(cn_raw)
        print(f"🔍 [patient_lookup] CN normalized: {cn_norm}")
        
        if not cn_norm:
            raise HTTPException(status_code=400, detail="CN ไม่ถูกต้อง")

        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                print(f"🔍 [patient_lookup] ค้นหา CN={cn_norm} ในตาราง patients...")
                cursor.execute(
                    "SELECT cn, hn, birth_date, gender, prefix, first_name, last_name "
                    "FROM patients WHERE cn_normalized = %s LIMIT 1",
                    (cn_norm,)
                )
                row = cursor.fetchone()
                print(f"🔍 [patient_lookup] ผลลัพธ์: {row}")
        finally:
            connection.close()

        if not row:
            print(f"⚠️ [patient_lookup] ไม่พบข้อมูลผู้ป่วยจาก CN={cn_norm}")
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูลผู้ป่วยจากเลข CN นี้")

        full_name = f"{row['prefix'] or ''}{row['first_name'] or ''} {row['last_name'] or ''}".strip()
        birth_date_str = row['birth_date'].strftime('%d/%m/%Y') if row['birth_date'] else None

        print(f"✅ [patient_lookup] สำเร็จ: {full_name}")
        return {
            "status": "success",
            "cn": row['cn'],
            "hn": row['hn'],
            "prefix": row['prefix'],
            "first_name": row['first_name'],
            "last_name": row['last_name'],
            "full_name": full_name,
            "birth_date": birth_date_str,
            "gender": row['gender'],
        }
    except HTTPException as e:
        print(f"❌ [patient_lookup] HTTPException: {e.detail}")
        raise
    except Exception as e:
        print(f"❌ [patient_lookup] Error: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.post("/api/patient-add")
async def patient_add(payload: PatientAddModel, current_user: dict = Depends(require_auth)):
    """
    🆕 เพิ่มข้อมูลผู้ป่วยใหม่เมื่อ CN ไม่เจอในระบบ
    ยิง CN ที่สแกนได้ + ข้อมูลพื้นฐาน (ชื่อ-สกุล/เพศ/วันเกิด/HN) มาให้เพิ่มเข้า patients table
    """
    try:
        cn_raw = payload.cn.strip()
        if not cn_raw:
            raise HTTPException(status_code=400, detail="ต้องระบุ CN")
        
        cn_norm = normalize_cn(cn_raw)
        if not cn_norm:
            raise HTTPException(status_code=400, detail="CN ไม่ถูกต้อง")

        first_name = payload.first_name.strip()
        last_name = payload.last_name.strip()
        hn = (payload.hn or "").strip() or None
        gender = (payload.gender or "").strip() or None
        birth_date_str = (payload.birth_date or "").strip() or None

        if not first_name:
            raise HTTPException(status_code=400, detail="ต้องระบุชื่อ")

        # แปลงวันเกิด (รับแบบ dd/mm/yyyy)
        birth_date = None
        if birth_date_str:
            try:
                from datetime import datetime
                birth_date = datetime.strptime(birth_date_str, "%d/%m/%Y").date()
            except (ValueError, TypeError) as e:
                raise HTTPException(status_code=400, detail=f"รูปแบบวันเกิดไม่ถูกต้อง (ต้อง dd/mm/yyyy): {str(e)}")

        print(f"📝 [patient_add] กำลังเพิ่ม CN={cn_norm}, ชื่อ={first_name} {last_name}")

        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                # ตรวจสอบว่า CN นี้มีอยู่แล้วหรือไม่ (กันสร้างซ้ำ)
                cursor.execute(
                    "SELECT id FROM patients WHERE cn_normalized = %s",
                    (cn_norm,)
                )
                if cursor.fetchone():
                    print(f"⚠️ [patient_add] CN={cn_norm} มีอยู่ในระบบแล้ว")
                    raise HTTPException(status_code=409, detail="CN นี้มีอยู่ในระบบแล้ว ให้ค้นหาอีกครั้ง")

                # บันทึกผู้ป่วยใหม่
                print(f"📝 [patient_add] INSERT: cn={cn_raw}, cn_normalized={cn_norm}, first_name={first_name}, last_name={last_name}, birth_date={birth_date}, gender={gender}, hn={hn}")
                cursor.execute(
                    """INSERT INTO patients (cn, cn_normalized, hn, first_name, last_name, birth_date, gender)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (cn_raw, cn_norm, hn, first_name, last_name, birth_date, gender)
                )
                connection.commit()
                print(f"✅ [patient_add] บันทึกสำเร็จ")
        finally:
            connection.close()

        # ส่งข้อมูลกลับเช่นเดียวกับ patient_lookup เพื่อให้ frontend แสดงการ์ดได้เลย
        full_name = f"{first_name} {last_name}".strip()
        return {
            "status": "success",
            "cn": cn_raw,
            "hn": hn,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "birth_date": birth_date_str,
            "gender": gender,
            "is_new": True
        }
    except HTTPException as e:
        print(f"❌ [patient_add] HTTPException: {e.detail}")
        raise
    except Exception as e:
        print(f"❌ [patient_add] Error: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"เกิดข้อผิดพลาด: {str(e)}")

@app.post("/api/register")
async def register_patient(payload: RegisterPatientModel, current_user: dict = Depends(require_auth)):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # 🌟 รองรับ 3 ประเภทคิว: Walk-in (W), นัดหมาย (A), จองผ่านมือถือ (M)
            # กรณีพนักงานลงทะเบียนหน้างานให้คนที่จองผ่านมือถือมาโดยตรง ไม่ผ่านหน้า general_appointments.html แยก
            QUEUE_PREFIX_MAP = {"walkin": "W", "appointment": "A", "mobile": "M"}
            prefix = QUEUE_PREFIX_MAP.get(payload.queue_type, "W")
            cursor.execute(
                "SELECT COUNT(*) as total FROM hospital_queues WHERE queue_type = %s AND DATE(created_at) = CURDATE()",
                (payload.queue_type,)
            )
            result = cursor.fetchone()
            queue_count = result['total'] + 1
            new_queue_num = f"{prefix}{queue_count:03d}"

            # ✅ ยกเลิกจุด VitalSign ออกจากระบบแล้ว — ไม่กำหนดโปรแกรมตรวจตอนลงทะเบียนอีกต่อไป
            # คนไข้จะได้แค่บัตรคิว (เลขคิว) เท่านั้น สถานะ "stations" ว่างเปล่าไว้ก่อน
            # แล้วไปกำหนดโปรแกรมตรวจจริงที่หน้า nurse_manage.html ทีหลัง (ผ่าน /api/assign-program)
            cn_norm = normalize_cn(payload.cn) if payload.cn else None

            cursor.execute(
                "INSERT INTO hospital_queues "
                "(queue_number, patient_name, current_station, queue_type, stations, cn, hn, birth_date, gender, priority_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
                (new_queue_num, payload.patient_name, "รอกำหนดโปรแกรมตรวจ", payload.queue_type,
                 json.dumps([]), cn_norm, payload.hn, payload.birth_date, payload.gender)
            )
            queue_id = cursor.lastrowid

            # ไม่สร้างแถว queue_stations ใดๆ เลยตอนนี้ — รอ /api/assign-program จาก nurse_manage.html
            # มากำหนดว่าคนไข้รายนี้ต้องตรวจจุดไหนบ้างก่อน ถึงจะสร้างแถวที่เกี่ยวข้อง

            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {
        "status": "success", 
        "queue_number": new_queue_num, 
        "patient_name": payload.patient_name,
        "queue_type": payload.queue_type,
        "cn": format_cn(cn_norm) if cn_norm else None,
        "hn": payload.hn
    }

@app.get("/api/queue-ticket/{queue_number}")
def get_queue_ticket(queue_number: str):
    """ดึงข้อมูลบัตรคิวฉบับเต็ม (ชื่อ/ประเภท/เวลา/ลำดับจุดบริการ) ใช้สำหรับหน้า 'พิมพ์บัตรคิวซ้ำ'"""
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT queue_number, patient_name, queue_type, stations,
                          DATE_FORMAT(created_at, '%%d/%%m/%%Y %%H:%%i:%%s') as created_at_th
                   FROM hospital_queues 
                   WHERE queue_number = %s AND DATE(created_at) = CURDATE()""",
                (queue_number,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในวันนี้")

            stations = json.loads(row['stations']) if row['stations'] else []
            station_labels = [STATION_LABELS.get(s, s) for s in stations]

            return {
                "status": "success",
                "queue_number": row['queue_number'],
                "patient_name": row['patient_name'],
                "queue_type": row['queue_type'],
                "created_at": row['created_at_th'],
                "stations": stations,
                "station_labels": station_labels
            }
    finally:
        connection.close()

@app.get("/api/pending-assignment")
def get_pending_assignment():
    """
    📋 รายชื่อคนไข้ที่ลงทะเบียนวันนี้แล้ว แต่ยังไม่ได้กำหนดโปรแกรมตรวจ
    เช็คจาก "ยังไม่มีแถวคิวจริงที่จุดตรวจที่ใช้งานอยู่ปัจจุบัน" (ไม่ใช้ field stations เพราะคิวเก่าก่อนอัปเดตระบบ
    อาจมีค่าค้างจากจุดที่ยกเลิกไปแล้ว เช่น VitalSign/Cashier ทำให้เช็คจาก field ตรงๆ พลาดได้)
    ใช้แสดงในหน้า nurse_manage.html เรียงตามลำดับที่มาก่อน-หลัง ให้พยาบาลคลิกเพื่อกำหนดโปรแกรมตรวจ
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT h.queue_number, h.patient_name, h.queue_type, h.cn, h.hn,
                       DATE_FORMAT(h.created_at, '%%H:%%i:%%s') as created_at_str
                FROM hospital_queues h
                WHERE DATE(h.created_at) = CURDATE()
                  AND h.is_cancelled = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM queue_stations qs
                      WHERE qs.queue_id = h.id
                        AND qs.station_name IN %s
                  )
                ORDER BY h.priority_time ASC, h.id ASC
            """, (tuple(GENERAL_TEST_STATIONS + ['Doctor']),))
            rows = cursor.fetchall()
        return {"status": "success", "count": len(rows), "patients": rows}
    finally:
        connection.close()

class CancelRegistrationModel(BaseModel):
    queue_number: str

@app.post("/api/cancel-registration")
async def cancel_registration(payload: CancelRegistrationModel, current_user: dict = Depends(require_auth)):
    """
    ยกเลิกคิวตั้งแต่ตอนลงทะเบียน — กรณีลงผิดประเภท (เช่น ตั้งใจลง Walk-in แต่ดันกดเลือก Mobile/นัดหมาย ผิดไป)
    ไม่ลบข้อมูลทิ้ง แค่ทำเครื่องหมาย is_cancelled=1 (เก็บไว้เป็นหลักฐาน/ตรวจสอบย้อนหลังได้)
    ⚠️ ทำได้เฉพาะคิวที่ "ยังไม่ได้กำหนดโปรแกรมตรวจเลย" เท่านั้น (ยังไม่มีแถว queue_stations ของจุดตรวจใดๆ)
    กันไม่ให้ไปยกเลิกคิวที่เริ่มกระบวนการตรวจไปแล้วโดยไม่ตั้งใจ
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE() AND is_cancelled = 0",
                (payload.queue_number,)
            )
            q_res = cursor.fetchone()
            if not q_res:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในวันนี้ หรือถูกยกเลิกไปแล้ว")

            cursor.execute(
                "SELECT COUNT(*) as cnt FROM queue_stations WHERE queue_id = %s AND station_name IN %s",
                (q_res['id'], tuple(GENERAL_TEST_STATIONS + ['Doctor']))
            )
            if cursor.fetchone()['cnt'] > 0:
                raise HTTPException(status_code=409, detail="คิวนี้ถูกกำหนดโปรแกรมตรวจไปแล้ว ไม่สามารถยกเลิกจากหน้านี้ได้ (ต้องยกเลิกทีละจุดที่หน้าเรียกคิวแทน)")

            cursor.execute(
                "UPDATE hospital_queues SET is_cancelled = 1, cancelled_at = NOW(), cancelled_by = %s WHERE id = %s",
                (current_user['username'], q_res['id'])
            )
            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "cancel-registration", payload.queue_number)
    return {"status": "success"}

@app.get("/api/queue-plan/{queue_number}")
def get_queue_plan(queue_number: str):
    """ดึงแผนตรวจเบื้องต้นที่ลงทะเบียนไว้ ใช้สำหรับ prefill checkbox ตอนกำหนดโปรแกรมตรวจที่หน้า nurse_manage.html"""
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT queue_number, patient_name, stations FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()",
                (queue_number,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในวันนี้")
            stations = json.loads(row['stations']) if row['stations'] else []
            return {
                "status": "success",
                "queue_number": row['queue_number'],
                "patient_name": row['patient_name'],
                "planned_stations": stations
            }
    finally:
        connection.close()

@app.get("/api/queue-status/{queue_number}")
def get_queue_status(queue_number: str):
    """ดึงสถานะปัจจุบันของทุกจุดตรวจของคิวนี้ ใช้แสดงในหน้า 'จัดการคิว / แก้ไขโปรแกรมตรวจ'"""
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, queue_number, patient_name, queue_type, stations, cn, DATE_FORMAT(created_at, '%%d/%%m/%%Y %%H:%%i:%%s') as created_at_str FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()",
                (queue_number,)
            )
            q_res = cursor.fetchone()
            if not q_res:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในวันนี้")

            cursor.execute(
                "SELECT station_name, status FROM queue_stations WHERE queue_id = %s AND LOWER(station_name) NOT LIKE 'doctor_%%'",
                (q_res['id'],)
            )
            rows = {r['station_name']: r['status'] for r in cursor.fetchall()}

            stations_status = []
            for st in GENERAL_TEST_STATIONS + ['Doctor']:
                stations_status.append({
                    "station": st,
                    "label": STATION_LABELS.get(st, st),
                    "status": rows.get(st, "NotAssigned")  # 🌟 แยกจาก "Lock" ชัดเจน — Lock คือ "กำหนดแล้ว รอสแกนเข้าคิว" ส่วนนี้คือ "ไม่เคยเลือกเลย"
                })

            return {
                "status": "success",
                "queue_number": q_res['queue_number'],
                "patient_name": q_res['patient_name'],
                "queue_type": q_res['queue_type'],
                "created_at": q_res['created_at_str'],
                "cn": q_res['cn'],
                "planned_stations": json.loads(q_res['stations']) if q_res['stations'] else [],
                "stations_status": stations_status
            }
    finally:
        connection.close()

@app.post("/api/assign-program")
async def assign_program(payload: AssignProgramModel, current_user: dict = Depends(require_auth)):
    """
    🌟 ขั้นตอน "กำหนดโปรแกรมตรวจ" — เรียกใช้หลังพยาบาลซักประวัติเสร็จ
    เพื่อยืนยัน/ปรับว่าคนไข้รายนี้ต้องเข้าจุดตรวจทั่วไปจุดไหนบ้างจริงๆ

    🔄 เปลี่ยนพฤติกรรม: จุดที่เลือกจะ "ยังไม่เข้าคิว" ทันที (คงเป็น Lock ไว้ก่อน)
    ต้องให้คนไข้/พยาบาลไปสแกน CN ที่หน้าจอของจุดนั้นจริงๆ ก่อน ถึงจะเข้าคิว (Pending) ที่จุดนั้น
    (ดู /api/checkin-station) — ป้องกันไม่ให้ทุกจุดขึ้นคิวพร้อมกันหมดทั้งที่คนไข้ยังไม่ได้ไปจริง ทำให้คิวมั่ว

    - จุดที่เลือก -> คงเป็น/ตั้งเป็น 'Lock' (รอสแกน CN ที่จุดนั้นเพื่อเข้าคิวจริง)
    - จุดที่ไม่ได้เลือก (แต่เคยถูกตั้งไว้ตอนลงทะเบียน) -> เปลี่ยนเป็น 'Skipped' (ไม่บล็อกขั้นตอนถัดไป)
    - อัปเดตรายการ stations ของคิวให้ตรงกับแผนจริง เพื่อให้ finish-queue คำนวณเงื่อนไขปลดล็อก Doctor/Cashier ถูกต้อง
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()",
                (payload.queue_number,)
            )
            q_res = cursor.fetchone()
            if not q_res:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในวันนี้")
            queue_id = q_res['id']

            chosen = []
            for s in payload.stations:
                norm = normalize_station_name(s)
                if norm in GENERAL_TEST_STATIONS and norm not in chosen:
                    chosen.append(norm)

            # ดึงสถานะปัจจุบันของแต่ละจุดตรวจทั่วไปก่อนแก้ไข เพื่อไม่ไปยุ่งกับจุดที่กำลังตรวจอยู่/ตรวจเสร็จแล้ว
            cursor.execute(
                "SELECT station_name, status FROM queue_stations WHERE queue_id = %s AND station_name IN %s",
                (queue_id, tuple(GENERAL_TEST_STATIONS))
            )
            current_status = {r['station_name']: r['status'] for r in cursor.fetchall()}
            LOCKED_OR_UNSTARTED = ('Lock', 'Skipped', None)
            IN_PROGRESS_OR_DONE = ('Calling', 'Pending', 'Hold', 'Completed')

            final_general_stations = []  # จุดที่ถือว่า "ยังอยู่ในกระบวนการ" จริงหลังแก้ไขครั้งนี้ (ใช้บันทึกลง stations JSON)

            for station in GENERAL_TEST_STATIONS:
                cur = current_status.get(station)

                if station in chosen:
                    # 🌟 ทุกจุดตรวจทั่วไป (รวมถึงจุดที่มีหลายห้องย่อยคู่ขนาน เช่น Blood) ใช้แถวเดียวชื่อ canonical
                    # (เช่น "Blood") เป็น "คิวกลาง" — ไม่ผูกกับห้องย่อยไหนตั้งแต่ตอนนี้
                    # ห้องย่อยจะถูกกำหนดจริงตอน "เรียกคิว" เท่านั้น (ห้องไหนว่างก่อนก็ดึงจากคิวกลางไปเรียกได้)
                    if cur in LOCKED_OR_UNSTARTED:
                        cursor.execute(
                            "UPDATE queue_stations SET status = 'Lock' WHERE queue_id = %s AND station_name = %s",
                            (queue_id, station)
                        )
                        if cursor.rowcount == 0:
                            cursor.execute(
                                "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, 'Lock')",
                                (queue_id, station)
                            )
                    # ถ้ากำลังตรวจอยู่ (Calling) หรือเข้าคิวแล้ว (Pending/Hold) หรือเสร็จแล้ว (Completed) -> ไม่ต้องแตะ
                    final_general_stations.append(station)
                else:
                    # ไม่ต้องการจุดนี้แล้ว -> ยกเลิก (Skipped) ได้เฉพาะจุดที่ยังไม่เริ่มตรวจ/ยังไม่เสร็จ
                    if cur in ('Lock', 'Pending', 'Hold', None):
                        cursor.execute(
                            "UPDATE queue_stations SET status = 'Skipped' WHERE queue_id = %s AND station_name = %s",
                            (queue_id, station)
                        )
                    elif cur in IN_PROGRESS_OR_DONE:
                        # จุดนี้กำลังตรวจอยู่หรือตรวจเสร็จไปแล้ว ยกเลิกย้อนหลังไม่ได้ -> ยังคงนับเป็นส่วนหนึ่งของแผนต่อไป
                        final_general_stations.append(station)

            final_stations = final_general_stations + (['Doctor'] if payload.include_doctor else [])
            cursor.execute(
                "UPDATE hospital_queues SET stations = %s WHERE id = %s",
                (json.dumps(final_stations), queue_id)
            )

            # 🌟 ถ้าต้องพบแพทย์ ต้องมั่นใจว่าแถว "Doctor" (คิวกลาง) ถูกสร้างไว้แล้ว
            # เพราะตอนลงทะเบียนตอนนี้ Doctor ไม่ได้ถูกติ๊กไว้ default (สร้างแถวเฉพาะตอนเลือกไว้ตั้งแต่แรกเท่านั้น)
            # ✅ ไม่สร้าง Doctor_1..Doctor_8 ล่วงหน้าอีกต่อไป — เป็นคิวกลางเหมือน Blood/Eye/Ear/EKG/Lung แล้ว
            # ห้องเฉพาะจะถูกกำหนดจริงตอน "เรียกคิว" เท่านั้น (ห้องไหนว่างก่อนก็เรียกได้)
            if payload.include_doctor:
                cursor.execute(
                    "SELECT status FROM queue_stations WHERE queue_id = %s AND station_name = 'Doctor'",
                    (queue_id,)
                )
                doctor_row = cursor.fetchone()
                if doctor_row is None:
                    cursor.execute(
                        "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, 'Doctor', 'Lock')",
                        (queue_id,)
                    )
                elif doctor_row['status'] in ('Skipped',):
                    # เคยถูกยกเลิกไว้ก่อนหน้า แต่ตอนนี้ต้องการอีกครั้ง -> เปิดกลับมาเป็น Lock
                    cursor.execute(
                        "UPDATE queue_stations SET status = 'Lock' WHERE queue_id = %s AND station_name = 'Doctor'",
                        (queue_id,)
                    )
                # ถ้ามีแถวอยู่แล้วและกำลังตรวจ/เข้าคิว/เสร็จแล้ว -> ไม่ต้องแตะ
            else:
                # ไม่ต้องการพบแพทย์แล้ว -> ยกเลิกได้เฉพาะถ้ายังไม่เริ่มตรวจ/ยังไม่เข้าคิว
                cursor.execute(
                    "UPDATE queue_stations SET status = 'Skipped' WHERE queue_id = %s AND station_name = 'Doctor' AND status IN ('Lock', 'Pending', 'Hold')",
                    (queue_id,)
                )

            # 🔄 ทั้งจุดตรวจทั่วไปและห้องหมอ ไม่ปลดล็อกอัตโนมัติแล้ว — ต้องสแกน QR/บาร์โค้ด CN
            # ที่หน้าจอของจุดนั้นก่อนเสมอ (ผ่าน /api/checkin-station) ปล่อยให้คงเป็น Lock ไว้ตามที่สร้างไว้ด้านบน
            # ✅ ยกเลิกจุดการเงิน (Cashier) ออกจากระบบทั้งหมดแล้ว — จบขั้นตอนที่ Doctor (หรือจุดตรวจทั่วไปจุดสุดท้ายถ้าไม่ต้องพบแพทย์)

            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success", "assigned_stations": final_general_stations}

class CheckinStationModel(BaseModel):
    code: str            # ข้อความที่สแกนได้จาก QR (ลิงก์ track.html?q=... หรือเลขคิวตรงๆ) หรือบาร์โค้ด CN บนใบคิว
    station_name: str

def extract_queue_number_from_scan(raw: str) -> str:
    """
    แกะเลขคิวออกจากสิ่งที่สแกนได้จาก QR Code บนบัตรคิว
    รองรับทั้งกรณีสแกนได้เป็นลิงก์เต็ม (เช่น .../track.html?q=W004) และกรณีพิมพ์/สแกนเป็นเลขคิวตรงๆ (เช่น W004)
    """
    if not raw:
        return ""
    raw = raw.strip()
    match = re.search(r'[?&]q=([A-Za-z0-9]+)', raw)
    if match:
        return match.group(1).upper()
    return raw.upper()

@app.post("/api/checkin-station")
async def checkin_station(payload: CheckinStationModel):
    """
    📍 "จองคิว" ที่จุดตรวจจริงๆ — สแกนที่หน้าจอของจุดนั้น รองรับ 2 แบบ:
    1. สแกน QR Code บนใบคิว (ลิงก์ track.html?q=...) — ใช้กับสแกนเนอร์กล้อง/มือถือ
    2. สแกนบาร์โค้ด CN บนใบคิว — ใช้กับเครื่องสแกนบาร์โค้ด 1D ทั่วไปที่มีอยู่แล้วตามจุดต่างๆ
    (คนไข้สแกนเองหรือพยาบาลสแกนให้ก็ได้) ใช้แทนการปลดล็อกทุกจุดพร้อมกันตอน assign-program
    ซึ่งทำให้คิวมั่ว เพราะคนไข้ยังไม่ได้ไปถึงจุดนั้นจริง

    รองรับทุกจุดตรวจ — ทั้งจุดห้องเดียว (XRay/Ultrasound/Mammogram) และจุดที่มีหลายห้องย่อยคู่ขนาน
    (Blood, Eye, Ear, EKG, Lung, Doctor) โดยสแกนเข้า "คิวกลาง" เสมอ ไม่ต้องเลือกห้องเฉพาะตอนสแกน
    ห้องจะถูกกำหนดจริงตอน "เรียกคิว" เท่านั้น (ห้องไหนว่างก่อนก็เรียกได้)

    ⚠️ ไม่ต้อง login เพราะคนไข้ต้องใช้เองได้ — แต่ทำได้แค่ "เข้าคิวจุดที่อยู่ในแผนของตัวเองอยู่แล้วเท่านั้น"
    """
    target_station = normalize_station_name(payload.station_name)

    valid_canonical_targets = GENERAL_TEST_STATIONS + ["Doctor"]
    if target_station not in valid_canonical_targets:
        raise HTTPException(status_code=400, detail="จุดนี้ไม่รองรับการจองคิวแบบสแกน")

    raw_code = (payload.code or "").strip()
    if not raw_code:
        raise HTTPException(status_code=400, detail="กรุณาสแกน QR Code หรือบาร์โค้ด CN บนใบคิว")

    queue_number = extract_queue_number_from_scan(raw_code)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # 1) ลองหาโดยตีความว่าเป็นเลขคิวก่อน (มาจาก QR Code)
            cursor.execute(
                "SELECT id, queue_number, patient_name FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()",
                (queue_number,)
            )
            q_res = cursor.fetchone()

            # 2) ถ้าหาไม่เจอ ลองตีความว่าเป็นบาร์โค้ด CN แทน (เอาคิวล่าสุดของ CN นี้ในวันนี้)
            if not q_res:
                cn_normalized = normalize_cn(raw_code)
                if cn_normalized:
                    cursor.execute(
                        """SELECT id, queue_number, patient_name FROM hospital_queues
                           WHERE cn = %s AND DATE(created_at) = CURDATE()
                           ORDER BY id DESC LIMIT 1""",
                        (cn_normalized,)
                    )
                    q_res = cursor.fetchone()

            if not q_res:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในระบบวันนี้ (สแกนไม่ถูกต้อง หรือเลยเวลาให้บริการแล้ว)")
            queue_id = q_res['id']

            # เช็คว่าจุดนี้อยู่ในแผนของคนไข้จริง และยังไม่เคยเข้าคิว (ต้องเป็น Lock เท่านั้น)
            cursor.execute(
                "SELECT status FROM queue_stations WHERE queue_id = %s AND station_name = %s",
                (queue_id, target_station)
            )
            st_res = cursor.fetchone()

            if not st_res:
                raise HTTPException(status_code=400, detail=f"คิวนี้ไม่มีจุด {STATION_LABELS.get(target_station, target_station)} อยู่ในแผนการตรวจ")

            if st_res['status'] in ('Pending', 'Hold', 'Calling'):
                raise HTTPException(status_code=409, detail="จองคิวจุดนี้ไปแล้ว กรุณารอเรียกคิว")
            if st_res['status'] == 'Completed':
                raise HTTPException(status_code=409, detail="ตรวจจุดนี้เสร็จไปแล้ว")
            if st_res['status'] == 'Skipped':
                raise HTTPException(status_code=409, detail="จุดนี้ถูกยกเลิกออกจากแผนการตรวจแล้ว")
            if st_res['status'] != 'Lock':
                raise HTTPException(status_code=400, detail="ไม่สามารถจองคิวจุดนี้ได้ในขณะนี้")

            cursor.execute(
                "UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND station_name = %s",
                (queue_id, target_station)
            )
            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))

    return {
        "status": "success",
        "queue_number": q_res['queue_number'],
        "patient_name": q_res['patient_name'],
        "station": target_station,
        "label": STATION_LABELS.get(target_station, target_station)
    }

# ==========================================================
# 📅 ระบบนัดเฉพาะ X-Ray / Ultrasound / Mammogram — แยกจากระบบคิวหลัก (walk-in/นัดทั่วไป)
# ใช้กับคนไข้ที่มาแค่ตรวจกลุ่มนี้อย่างเดียว ไม่เกี่ยวกับ VitalSign/โปรแกรมตรวจอื่น
#
# หลักการ: Import ล่วงหน้า -> รายชื่อขึ้นเป็น "scheduled" (ยังไม่มีเลขคิว)
# พอมาถึงจริง พนักงานค้นชื่อ -> กดยืนยัน -> ออกเลขคิวตอนนั้น พร้อมตั้ง priority_time = เวลานัดที่ตั้งไว้
# ทำให้จัดคิวราวกับมาถึงตรงเวลานัด แซงหน้า walk-in ที่มาทีหลังเวลานัดของตัวเองได้ถูกต้อง
# ==========================================================

XRAY_EXAM_TYPES = ['XRay', 'Ultrasound', 'Mammogram']

@app.post("/api/xray-appointments/bulk-import")
async def bulk_import_xray_appointments(
    file: UploadFile = File(...),
    appointment_date: str = Form(...),  # YYYY-MM-DD
    current_user: dict = Depends(require_auth)
):
    """
    นำเข้ารายชื่อนัด X-Ray/Ultrasound/Mammogram จากไฟล์ Excel (.xlsx) ล่วงหน้า
    รูปแบบคอลัมน์ที่ต้องมี (แถวแรกเป็นหัวตาราง): ชื่อ-สกุล | เวลานัด (HH:MM) | ประเภทตรวจ | CN | HN
    """
    if openpyxl is None:
        raise HTTPException(
            status_code=500,
            detail="เครื่อง Server นี้ยังไม่ได้ติดตั้ง openpyxl (ใช้อ่านไฟล์ Excel) — ต้องติดตั้งก่อนใช้ฟีเจอร์นี้"
        )

    if not file.filename.endswith(('.xlsx', '.xlsm')):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ .xlsx เท่านั้น")

    try:
        contents = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
        ws = wb.active
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"เปิดไฟล์ Excel ไม่สำเร็จ: {str(e)}")

    EXAM_TYPE_MAP = {
        'xray': 'XRay', 'x-ray': 'XRay', 'เอกซเรย์': 'XRay', 'เอ็กซเรย์': 'XRay',
        'ultrasound': 'Ultrasound', 'อัลตราซาวด์': 'Ultrasound',
        'mammogram': 'Mammogram', 'แมมโมแกรม': 'Mammogram',
    }

    imported = []
    errors = []
    skipped_blank_rows = []

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # แถวที่ 1 = หัวตาราง ข้ามไป เริ่มอ่านจริงแถวที่ 2
            for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                if not row or not row[0]:
                    skipped_blank_rows.append(row_idx)  # รายงานให้เห็นชัดว่าข้ามแถวไหนไปบ้าง (กันเข้าใจผิดว่าข้อมูลหาย)
                    continue

                try:
                    patient_name = str(row[0]).strip()
                    appointment_time_raw = row[1]
                    exam_type_raw = str(row[2]).strip().lower() if len(row) > 2 and row[2] else ""
                    cn_raw = str(row[3]).strip() if len(row) > 3 and row[3] else None
                    hn_raw = str(row[4]).strip() if len(row) > 4 and row[4] else None

                    exam_type = EXAM_TYPE_MAP.get(exam_type_raw)
                    if not exam_type:
                        errors.append(f"แถว {row_idx}: ไม่รู้จักประเภทตรวจ '{exam_type_raw}' (ต้องเป็น XRay/Ultrasound/Mammogram)")
                        continue

                    # แปลงเวลานัด รองรับทั้ง datetime.time object (Excel เก็บเวลาแบบนี้) และ string เช่น "09:00"
                    if isinstance(appointment_time_raw, datetime.time):
                        appt_time_str = appointment_time_raw.strftime("%H:%M:%S")
                    elif isinstance(appointment_time_raw, datetime.datetime):
                        appt_time_str = appointment_time_raw.strftime("%H:%M:%S")
                    else:
                        appt_time_str = str(appointment_time_raw).strip()

                    cn_norm = normalize_cn(cn_raw) if cn_raw else None

                    cursor.execute(
                        """INSERT INTO xray_appointments
                           (appointment_date, appointment_time, patient_name, cn, hn, exam_type, status)
                           VALUES (%s, %s, %s, %s, %s, %s, 'scheduled')""",
                        (appointment_date, appt_time_str, patient_name, cn_norm, hn_raw, exam_type)
                    )
                    imported.append(patient_name)

                except Exception as row_err:
                    errors.append(f"แถว {row_idx}: {str(row_err)}")

            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "xray-bulk-import", f"date={appointment_date}, imported={len(imported)}, errors={len(errors)}, skipped_blank={len(skipped_blank_rows)}")

    return {
        "status": "success",
        "imported_count": len(imported),
        "error_count": len(errors),
        "errors": errors,
        "skipped_blank_count": len(skipped_blank_rows),
        "skipped_blank_rows": skipped_blank_rows
    }

@app.get("/api/xray-appointments/template")
async def download_xray_template(current_user: dict = Depends(require_auth)):
    """
    ดาวน์โหลดไฟล์ Excel ต้นแบบ (Template) ที่มีหัวตารางถูกต้องพร้อมตัวอย่าง 1 แถว
    ป้องกันปัญหารูปแบบไฟล์ผิด (เช่น เซลล์ merge, ลำดับคอลัมน์สลับ) ตั้งแต่ต้นทาง
    """
    if openpyxl is None:
        raise HTTPException(status_code=500, detail="เครื่อง Server นี้ยังไม่ได้ติดตั้ง openpyxl")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "นัด X-Ray"

    headers = ["ชื่อ-สกุล", "เวลานัด", "ประเภทตรวจ", "CN", "HN"]
    ws.append(headers)

    # ตัวอย่างแถวข้อมูล (ลบออกก่อนใส่ข้อมูลจริง)
    ws.append(["สมชาย ใจดี", "09:00", "XRay", "15C15103215", "H12345"])
    ws.append(["สมหญิง รักดี", "09:30", "Ultrasound", "", ""])
    ws.append(["มานี มีนา", "10:00", "Mammogram", "", ""])

    # ปรับความกว้างคอลัมน์ให้อ่านง่าย
    widths = [22, 12, 14, 16, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    # ทำหัวตารางตัวหนา
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=xray_appointment_template.xlsx"}
    )

@app.get("/api/xray-appointments/today")
async def get_today_xray_appointments(date: Optional[str] = None, current_user: dict = Depends(require_auth)):
    """รายชื่อนัด X-Ray/Ultrasound/Mammogram ของวันที่ระบุ (ไม่ระบุ = วันนี้) ใช้ค้นหาตอนคนไข้มาถึง"""
    target_date = date or datetime.date.today().isoformat()

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, appointment_time, patient_name, cn, hn, exam_type, status, queue_number
                   FROM xray_appointments
                   WHERE appointment_date = %s
                   ORDER BY appointment_time ASC""",
                (target_date,)
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    for r in rows:
        at = r['appointment_time']
        if isinstance(at, datetime.timedelta):
            # 🐛 แก้บั๊ก: pymysql คืนค่าคอลัมน์ TIME เป็น datetime.timedelta (ไม่ใช่ datetime.time)
            # ถ้าไม่แปลงตรงนี้ JSON จะ serialize เป็น "จำนวนวินาทีทั้งหมด" แทน (เช่น 52200 แทน 14:30:00)
            total_seconds = int(at.total_seconds())
            h, remainder = divmod(total_seconds, 3600)
            m, s = divmod(remainder, 60)
            r['appointment_time'] = f"{h:02d}:{m:02d}:{s:02d}"
        elif hasattr(at, 'strftime'):
            r['appointment_time'] = str(at)

    return {"status": "success", "date": target_date, "appointments": rows}

class ConfirmXrayAppointmentModel(BaseModel):
    appointment_id: int

@app.post("/api/xray-appointments/confirm")
async def confirm_xray_appointment(payload: ConfirmXrayAppointmentModel, current_user: dict = Depends(require_auth)):
    """
    ยืนยันว่าคนไข้ที่นัดไว้มาถึงแล้ว — ออกเลขคิวทันที และตั้ง priority_time = เวลานัดที่ตั้งไว้
    (ไม่ใช่เวลาที่กดยืนยันจริง) ทำให้จัดลำดับคิวถูกต้องราวกับมาถึงตรงเวลานัด แซงหน้า walk-in ที่มาสายกว่าเวลานัดนี้ได้
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM xray_appointments WHERE id = %s AND status = 'scheduled'",
                (payload.appointment_id,)
            )
            appt = cursor.fetchone()
            if not appt:
                raise HTTPException(status_code=404, detail="ไม่พบรายการนัดนี้ หรือถูกยืนยันไปแล้ว")

            # ออกเลขคิวใหม่ (prefix 'A' เหมือนคิวนัดทั่วไป)
            cursor.execute(
                "SELECT COUNT(*) as total FROM hospital_queues WHERE queue_type = 'appointment' AND DATE(created_at) = CURDATE()"
            )
            queue_count = cursor.fetchone()['total'] + 1
            new_queue_num = f"A{queue_count:03d}"

            # 🌟 priority_time = วันที่วันนี้ + เวลานัดที่ตั้งไว้ (ไม่ใช่เวลาปัจจุบันที่กดยืนยัน)
            # 🐛 แก้บั๊กสำคัญ: pymysql คืนค่าคอลัมน์ TIME เป็น datetime.timedelta ไม่ใช่ datetime.time
            # เดิมเช็ค hasattr(appt_time, 'hour') ซึ่ง timedelta ไม่มี attribute นี้ ทำให้ตกไป else
            # แล้ว fallback เป็น datetime.now() เสมอ — เท่ากับฟีเจอร์ "แซงคิวตามเวลานัด" ไม่เคยทำงานจริงเลย
            appt_date = appt['appointment_date']
            appt_time = appt['appointment_time']
            if isinstance(appt_time, datetime.timedelta):
                total_seconds = int(appt_time.total_seconds())
                h, remainder = divmod(total_seconds, 3600)
                m, s = divmod(remainder, 60)
                appt_time_obj = datetime.time(hour=h % 24, minute=m, second=s)
                priority_datetime = datetime.datetime.combine(appt_date, appt_time_obj)
            elif hasattr(appt_time, 'hour'):
                priority_datetime = datetime.datetime.combine(appt_date, appt_time)
            else:
                priority_datetime = datetime.datetime.now()

            exam_type = appt['exam_type']

            cursor.execute(
                """INSERT INTO hospital_queues
                   (queue_number, patient_name, current_station, queue_type, stations, cn, hn, priority_time)
                   VALUES (%s, %s, %s, 'appointment', %s, %s, %s, %s)""",
                (new_queue_num, appt['patient_name'], exam_type, json.dumps([exam_type]),
                 appt['cn'], appt['hn'], priority_datetime)
            )
            queue_id = cursor.lastrowid

            # เข้าคิวทันที (Pending) — การยืนยันมาถึงคือการเช็คอินอยู่แล้ว ไม่ต้องสแกนซ้ำอีกรอบ
            cursor.execute(
                "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, 'Pending')",
                (queue_id, exam_type)
            )

            cursor.execute(
                "UPDATE xray_appointments SET status = 'confirmed', queue_number = %s, confirmed_at = NOW() WHERE id = %s",
                (new_queue_num, payload.appointment_id)
            )

            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "xray-confirm-arrival", f"{appt['patient_name']} -> {new_queue_num} ({exam_type})")

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))

    return {
        "status": "success",
        "queue_number": new_queue_num,
        "patient_name": appt['patient_name'],
        "exam_type": exam_type,
        "label": STATION_LABELS.get(exam_type, exam_type)
    }

class XrayWalkinModel(BaseModel):
    patient_name: str
    exam_type: str  # 'XRay', 'Ultrasound', 'Mammogram'
    cn: Optional[str] = None
    hn: Optional[str] = None

@app.post("/api/xray-appointments/walkin")
async def xray_walkin(payload: XrayWalkinModel, current_user: dict = Depends(require_auth)):
    """
    🚶 Walk-in ด่วนสำหรับคนไข้ที่มาตรวจ X-Ray/Ultrasound/Mammogram อย่างเดียว
    โดยไม่ได้ผ่านเคาน์เตอร์ลงทะเบียนหลัก และไม่ได้นัดล่วงหน้ามาก่อน
    ออกเลขคิว (prefix 'W') ให้ทันที เข้าคิวเลย ไม่ต้องผ่านขั้นตอนนัด/ยืนยันเหมือนคนที่นัดไว้ล่วงหน้า
    """
    exam_type = normalize_station_name(payload.exam_type)
    if exam_type not in ('XRay', 'Ultrasound', 'Mammogram'):
        raise HTTPException(status_code=400, detail="ประเภทตรวจต้องเป็น XRay, Ultrasound หรือ Mammogram เท่านั้น")

    if not payload.patient_name or not payload.patient_name.strip():
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อ-สกุลผู้ป่วย")

    cn_norm = normalize_cn(payload.cn) if payload.cn else None

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # ออกเลขคิวแบบ Walk-in (prefix 'W' เหมือนคิวทั่วไปที่ไม่มีนัด)
            cursor.execute(
                "SELECT COUNT(*) as total FROM hospital_queues WHERE queue_type = 'walkin' AND DATE(created_at) = CURDATE()"
            )
            queue_count = cursor.fetchone()['total'] + 1
            new_queue_num = f"W{queue_count:03d}"

            cursor.execute(
                """INSERT INTO hospital_queues
                   (queue_number, patient_name, current_station, queue_type, stations, cn, hn, priority_time)
                   VALUES (%s, %s, %s, 'walkin', %s, %s, %s, NOW())""",
                (new_queue_num, payload.patient_name.strip(), exam_type, json.dumps([exam_type]), cn_norm, payload.hn)
            )
            queue_id = cursor.lastrowid

            # เข้าคิวทันที (Pending) — มาถึงแล้วจริง ไม่ต้องสแกนซ้ำอีกรอบ
            cursor.execute(
                "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, 'Pending')",
                (queue_id, exam_type)
            )

            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "xray-walkin", f"{payload.patient_name} -> {new_queue_num} ({exam_type})")

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))

    return {
        "status": "success",
        "queue_number": new_queue_num,
        "patient_name": payload.patient_name.strip(),
        "exam_type": exam_type,
        "label": STATION_LABELS.get(exam_type, exam_type)
    }

# ==========================================================
# 📅 ระบบจองคิวออนไลน์ทั่วไป — คนไข้กรอกเองจากมือถือผ่าน QR Code (public_track.py บนเครื่อง notebook)
# ต่างจากระบบนัด X-Ray ตรงที่ "ไม่เลือกจุดตรวจ" — โปรแกรมตรวจจริงไปตัดสินใจหน้าเคาน์เตอร์หลังซักประวัติ
# เหมือนคนไข้ walk-in ปกติทุกประการ เพียงแต่รู้ล่วงหน้าว่าใครจะมาบ้าง ลดเวลาการกรอกข้อมูลหน้าเคาน์เตอร์
# ==========================================================

@app.get("/api/general-appointments/today")
async def get_today_general_appointments(date: Optional[str] = None, current_user: dict = Depends(require_auth)):
    """รายชื่อคนไข้ที่จองคิวออนไลน์ไว้ล่วงหน้า ของวันที่ระบุ (ไม่ระบุ = วันนี้) ใช้ค้นหาตอนคนไข้มาถึงจริง"""
    target_date = date or datetime.date.today().isoformat()

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, appointment_time, patient_name, phone, cn, hn, note, status, queue_number
                   FROM general_appointments
                   WHERE appointment_date = %s
                   ORDER BY appointment_time IS NULL, appointment_time ASC, id ASC""",
                (target_date,)
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    for r in rows:
        at = r['appointment_time']
        if isinstance(at, datetime.timedelta):
            # 🐛 pymysql คืนคอลัมน์ TIME เป็น timedelta ไม่ใช่ time — แปลงเป็น HH:MM:SS ก่อนส่งกลับ (บั๊กเดิมที่เจอมาแล้ว)
            total_seconds = int(at.total_seconds())
            h, remainder = divmod(total_seconds, 3600)
            m, s = divmod(remainder, 60)
            r['appointment_time'] = f"{h:02d}:{m:02d}:{s:02d}"
        elif hasattr(at, 'strftime'):
            r['appointment_time'] = str(at)

    return {"status": "success", "date": target_date, "appointments": rows}

class ConfirmGeneralAppointmentModel(BaseModel):
    appointment_id: int

@app.post("/api/general-appointments/confirm")
async def confirm_general_appointment(payload: ConfirmGeneralAppointmentModel, current_user: dict = Depends(require_auth)):
    """
    ยืนยันว่าคนไข้ที่จองคิวออนไลน์ไว้มาถึงแล้ว — ออกเลขคิวทันที (เหมือน walk-in ปกติ)
    ไม่กำหนดโปรแกรมตรวจใดๆ ตอนนี้ — ไปกำหนดที่หน้า nurse_manage.html ทีหลังตามปกติ
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM general_appointments WHERE id = %s AND status = 'scheduled'",
                (payload.appointment_id,)
            )
            appt = cursor.fetchone()
            if not appt:
                raise HTTPException(status_code=404, detail="ไม่พบรายการจองนี้ หรือถูกยืนยันไปแล้ว")

            # ออกเลขคิวแบบนัดหมาย (prefix 'A' เพราะจองล่วงหน้ามา — "Mobile" หมายถึงหน่วยเคลื่อนที่ ไม่เกี่ยวกับการจองออนไลน์)
            cursor.execute(
                "SELECT COUNT(*) as total FROM hospital_queues WHERE queue_type = 'appointment' AND DATE(created_at) = CURDATE()"
            )
            queue_count = cursor.fetchone()['total'] + 1
            new_queue_num = f"A{queue_count:03d}"

            cn_norm = normalize_cn(appt['cn']) if appt['cn'] else None

            cursor.execute(
                """INSERT INTO hospital_queues
                   (queue_number, patient_name, current_station, queue_type, stations, cn, hn, priority_time)
                   VALUES (%s, %s, %s, 'appointment', %s, %s, %s, NOW())""",
                (new_queue_num, appt['patient_name'], "รอกำหนดโปรแกรมตรวจ", json.dumps([]), cn_norm, appt['hn'])
            )
            queue_id = cursor.lastrowid
            # ไม่สร้างแถว queue_stations ใดๆ เลย — รอ /api/assign-program จาก nurse_manage.html เหมือนคนไข้ปกติทุกคน

            cursor.execute(
                "UPDATE general_appointments SET status = 'confirmed', queue_number = %s, confirmed_at = NOW() WHERE id = %s",
                (new_queue_num, payload.appointment_id)
            )

            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "general-appointment-confirm", f"{appt['patient_name']} -> {new_queue_num}")

    return {
        "status": "success",
        "queue_number": new_queue_num,
        "patient_name": appt['patient_name']
    }


@app.post("/api/call-queue")
async def call_queue(payload: CallQueueModel, current_user: dict = Depends(require_auth)):
    target_station = normalize_station_name(payload.station_name)
    canonical_target = get_canonical_station(target_station)

    # 🚪 ถ้าห้องนี้ถูกปิดไว้ (เช่น พยาบาลลาป่วยกะทันหัน) ห้ามเรียกคิวเข้าห้องนี้
    if target_station in CLOSED_ROOMS:
        raise HTTPException(status_code=403, detail=f"ห้อง {STATION_LABELS.get(target_station, target_station)} ถูกปิดใช้งานชั่วคราวอยู่ ไม่สามารถเรียกคิวได้")

    log_audit(current_user['username'], "call-queue", f"{payload.queue_number} @ {target_station}")

    # 🌟 จุดที่มีหลายห้องย่อยคู่ขนาน (Blood/Eye/Ear/EKG/Lung/Doctor) ใช้ "คิวกลาง" (ชื่อ canonical เช่น 'Blood', 'Doctor')
    # เก็บสถานะ Pending/Hold ไว้ร่วมกัน — พอถูกเรียกจากห้องใดห้องหนึ่ง ต้อง "ย้าย" แถวจาก canonical
    # ไปเป็นชื่อห้องเฉพาะ (เช่น Blood -> Blood_2, Doctor -> Doctor_3) ตอนกดเรียกเท่านั้น ไม่ใช่ตั้งแต่ตอนสแกนรับคิว
    is_shared_pool_station = (canonical_target in MULTI_ROOM_STATIONS) and (target_station != canonical_target)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT station_name 
                FROM queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                WHERE q.queue_number = %s AND qs.status = 'Calling' AND LOWER(qs.station_name) != LOWER(%s) AND DATE(q.created_at) = CURDATE()
            """, (payload.queue_number, target_station))
            
            already_calling = cursor.fetchone()
            if already_calling:
                return {
                    "status": "error", 
                    "message": f"คิว {payload.queue_number} กำลังรับการตรวจอยู่ที่จุด [{already_calling['station_name']}] ไม่สามารถเรียกซ้ำได้"
                }

            if is_shared_pool_station:
                # ถ้าห้องนี้เรียกคนอื่นค้างอยู่ (Calling) ให้คืนคนเดิมกลับไปคิวกลาง (canonical) เป็น Pending ก่อน
                cursor.execute("""
                    UPDATE queue_stations qs
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    SET qs.station_name = %s, qs.status = 'Pending'
                    WHERE LOWER(qs.station_name) = LOWER(%s) AND qs.status = 'Calling' AND DATE(q.created_at) = CURDATE()
                """, (canonical_target, target_station))

                # ย้ายคิวที่ต้องการเรียกจากคิวกลาง (canonical) มาเป็นห้องเฉพาะนี้ + ตั้งเป็น Calling
                # เงื่อนไข status IN ('Pending','Hold') ป้องกัน race condition — ถ้าห้องอื่นเรียกไปพร้อมกันจริง
                # แถวนี้จะไม่อยู่ในสถานะ Pending/Hold แล้ว rowcount จะเป็น 0 กันเรียกซ้อนกัน
                cursor.execute("""
                    UPDATE queue_stations qs 
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    SET qs.station_name = %s, qs.status = 'Calling' 
                    WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) 
                      AND qs.status IN ('Pending', 'Hold') AND DATE(q.created_at) = CURDATE()
                """, (target_station, payload.queue_number, canonical_target))

                if cursor.rowcount == 0:
                    return {
                        "status": "error",
                        "message": f"คิว {payload.queue_number} ไม่ได้อยู่ในคิวกลางที่รอเรียกแล้ว (อาจถูกห้องอื่นเรียกไปพร้อมกัน)"
                    }

            else:
                # จุดห้องเดียว (XRay/Ultrasound/Mammogram) — ทำงานแบบเดิม
                cursor.execute("""
                    UPDATE queue_stations qs
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    SET qs.status = 'Pending' 
                    WHERE LOWER(qs.station_name) = LOWER(%s) AND qs.status = 'Calling' AND DATE(q.created_at) = CURDATE()
                """, (target_station,))

                cursor.execute("""
                    UPDATE queue_stations qs 
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    SET qs.status = 'Calling' 
                    WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) AND DATE(q.created_at) = CURDATE()
                """, (payload.queue_number, target_station))
            
            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.post("/api/recall-queue")
async def recall_queue(payload: CallQueueModel, current_user: dict = Depends(require_auth)):
    """
    🔁 เรียกคิวซ้ำ (Recall) — ใช้เมื่อคนไข้ไม่ได้ยิน/ไม่มาตามคิวที่เรียกไปแล้ว
    ไม่เปลี่ยนสถานะคิว (ยังคง 'Calling' เหมือนเดิม) แค่ยิง event พิเศษไปให้จอ TV
    เล่นเสียงประกาศซ้ำ + กระพริบจอซ้ำสำหรับคิว/สถานีนี้โดยเฉพาะ
    """
    target_station = normalize_station_name(payload.station_name)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT qs.id 
                FROM queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) 
                  AND qs.status = 'Calling' AND DATE(q.created_at) = CURDATE()
            """, (payload.queue_number, target_station))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail=f"คิว {payload.queue_number} ไม่ได้อยู่ในสถานะกำลังเรียกที่จุดนี้ ไม่สามารถเรียกซ้ำได้")

            cursor.execute("UPDATE queue_stations SET updated_at = NOW() WHERE id = %s", (row['id'],))
            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    # แนบ event เรียกซ้ำ (nonce กันการเล่นเสียงซ้ำ/สับสนจาก broadcast เดียวกัน)
    updated_data["recall"] = {
        "station": target_station,
        "queue_number": payload.queue_number,
        "patient_name": updated_data.get("current_calls_names", {}).get(target_station, ""),
        "nonce": str(uuid.uuid4())
    }
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.post("/api/hold-queue")
async def hold_queue(payload: CallQueueModel, current_user: dict = Depends(require_auth)):
    target_station = normalize_station_name(payload.station_name)
    canonical_target = get_canonical_station(target_station)
    log_audit(current_user['username'], "hold-queue", f"{payload.queue_number} @ {target_station}")

    # 🌟 จุดคิวกลาง (Blood/Eye/Ear/EKG/Lung/Doctor) — พอพักคิว ต้องคืนชื่อกลับเป็น canonical (เช่น Blood, Doctor)
    # ให้กลับไปอยู่ในคิวกลางที่ห้องไหนก็เรียกต่อได้ ไม่ใช่ค้างอยู่แค่ห้องที่พักไว้ห้องเดียว
    is_shared_pool_station = (canonical_target in MULTI_ROOM_STATIONS) and (target_station != canonical_target)
    rename_to = canonical_target if is_shared_pool_station else target_station

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                SET qs.station_name = %s, qs.status = 'Hold'
                WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) AND DATE(q.created_at) = CURDATE()
            """, (rename_to, payload.queue_number, target_station))
            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.post("/api/cancel-checkin")
async def cancel_checkin(payload: CallQueueModel, current_user: dict = Depends(require_auth)):
    """
    ยกเลิกการเข้าคิวที่จุดนี้ กรณีสแกนผิดที่/ผิดคน — คืนสถานะกลับเป็น "Lock" เหมือนยังไม่เคยสแกนเข้ามาเลย
    (ยังอยู่ในแผนตรวจของคนไข้เหมือนเดิม แค่ต้องไปสแกนเข้าคิวใหม่ให้ถูกจุด ไม่ใช่การยกเลิกออกจากแผนแบบถาวร)
    ใช้ได้เฉพาะสถานะ Pending/Hold เท่านั้น — กันไม่ให้ยกเลิกคิวที่กำลังตรวจอยู่หรือตรวจเสร็จแล้วโดยไม่ตั้งใจ
    """
    target_station = normalize_station_name(payload.station_name)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                SET qs.status = 'Lock'
                WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s)
                  AND qs.status IN ('Pending', 'Hold') AND DATE(q.created_at) = CURDATE()
            """, (payload.queue_number, target_station))

            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในสถานะที่ยกเลิกได้ (ต้องเป็นคิวที่ยังไม่เริ่มตรวจ/ยังไม่เสร็จเท่านั้น)")

            connection.commit()
    finally:
        connection.close()

    log_audit(current_user['username'], "cancel-checkin", f"{payload.queue_number} @ {target_station}")

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.post("/api/finish-queue")
async def finish_queue(payload: CallQueueModel, current_user: dict = Depends(require_auth)):
    target_station = normalize_station_name(payload.station_name)
    log_audit(current_user['username'], "finish-queue", f"{payload.queue_number} @ {target_station}")
    
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # อัปเดตสถานีปัจจุบันเป็น Completed
            cursor.execute("""
                UPDATE queue_stations qs 
                JOIN hospital_queues q ON qs.queue_id = q.id
                SET qs.status = 'Completed' 
                WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) AND DATE(q.created_at) = CURDATE()
            """, (payload.queue_number, target_station))
            
            cursor.execute("SELECT id, stations FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()", (payload.queue_number,))
            q_res = cursor.fetchone()
            if q_res:
                queue_id = q_res['id']
                st_name = target_station.lower()
                
                assigned_stations = json.loads(q_res['stations']) if q_res['stations'] else []
                assigned_lower = [s.lower() for s in assigned_stations]

                # กรณีจุดที่เพิ่งตรวจเสร็จเป็นสถานีตรวจทั่วไป (Blood, Eye, Ear, Lung, EKG, XRay, Ultrasound, Mammogram)
                # หรือห้องย่อยของสถานีเหล่านั้น (เช่น Blood_2) — ใช้ canonical station ในการเช็ค
                canonical_lower = get_canonical_station(target_station).lower()
                if canonical_lower in ['blood', 'eye', 'ear', 'lung', 'ekg', 'xray', 'ultrasound', 'mammogram', 'obgyn']:
                    # ตรวจสอบว่ามีสถานีตรวจทั่วไปอื่นๆ ในรายการที่ยังตรวจไม่เสร็จ (ยังไม่เป็น Completed) หรือไม่
                    # จุดที่มีหลายห้องย่อย (multi-room) ถือว่า "เสร็จ" เมื่อห้องย่อยห้องใดห้องหนึ่งเป็น Completed
                    uncompleted_count = 0
                    for assigned_st in assigned_stations:
                        if assigned_st not in GENERAL_TEST_STATIONS:
                            continue
                        if assigned_st in MULTI_ROOM_STATIONS:
                            cursor.execute(
                                "SELECT COUNT(*) as cnt FROM queue_stations WHERE queue_id = %s AND station_name IN %s AND status = 'Completed'",
                                (queue_id, tuple(MULTI_ROOM_STATIONS[assigned_st]))
                            )
                            is_done = cursor.fetchone()['cnt'] > 0
                        else:
                            cursor.execute(
                                "SELECT status FROM queue_stations WHERE queue_id = %s AND station_name = %s",
                                (queue_id, assigned_st)
                            )
                            row = cursor.fetchone()
                            is_done = bool(row and row['status'] in ('Completed', 'Skipped'))
                        if not is_done:
                            uncompleted_count += 1

                    # ถ้าตรวจสถานีทั่วไปหมดทุกจุดแล้ว และไม่ต้องพบแพทย์ -> จบขั้นตอนการรับบริการทั้งหมดแล้ว
                    # (ไม่มีจุดการเงิน/Cashier ให้ปลดล็อกต่ออีกแล้ว — ยกเลิกออกจากระบบทั้งหมด)
                    # ถ้าต้องพบแพทย์ ห้องหมอไม่ปลดล็อกอัตโนมัติ ต้องสแกน QR/บาร์โค้ด CN ที่ห้องนั้นเอง ปล่อยเป็น Lock ไว้เฉยๆ
                
                # กรณีจุดที่เพิ่งตรวจเสร็จเป็นห้องตรวจแพทย์ (Doctor หรือ Doctor_1 - Doctor_8)
                elif st_name == "doctor" or st_name.startswith("doctor_"):
                    # ปิดเฉพาะ "ห้องที่ตรวจจริง" เป็น Completed (คนไข้ไปตรวจแค่ห้องเดียว ไม่ใช่ทุกห้อง)
                    cursor.execute("""
                        UPDATE queue_stations 
                        SET status = 'Completed' 
                        WHERE queue_id = %s AND LOWER(station_name) = %s
                    """, (queue_id, st_name))

                    # ห้องหมออื่นที่ไม่ได้ใช้ (ทั้งชื่อ 'Doctor' ทั่วไป และห้องอื่นๆ ที่เหลือ) ปิดเป็น Skipped แทน
                    # 🌟 สำคัญ: ต้องใช้ Skipped ไม่ใช่ Completed มิเช่นนั้นสถิติ "จำนวนที่ตรวจเสร็จ" จะนับผิด
                    # (เดิมปิดเป็น Completed ทั้งหมด ทำให้คนไข้ 1 คนถูกนับเป็น 9 ครั้งที่ห้องหมอ)
                    cursor.execute("""
                        UPDATE queue_stations 
                        SET status = 'Skipped' 
                        WHERE queue_id = %s 
                          AND (LOWER(station_name) = 'doctor' OR LOWER(station_name) LIKE 'doctor_%%')
                          AND LOWER(station_name) != %s
                          AND status NOT IN ('Completed', 'Skipped')
                    """, (queue_id, st_name))
                    # ✅ ตรวจแพทย์เสร็จแล้ว = จบขั้นตอนการรับบริการทั้งหมด (ไม่มีจุดการเงิน/Cashier ต่ออีกแล้ว)

            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.get("/api/today-queues")
def get_today_queues(search: Optional[str] = None):
    """ค้นหาคิววันนี้ ด้วยเลขคิวหรือชื่อคนไข้ (บางส่วนก็ค้นเจอ) ใช้สำหรับหน้าพิมพ์บัตรคิวซ้ำ"""
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            if search:
                like = f"%{search}%"
                cursor.execute(
                    """SELECT queue_number, patient_name, queue_type 
                       FROM hospital_queues 
                       WHERE DATE(created_at) = CURDATE() AND is_cancelled = 0
                         AND (queue_number LIKE %s OR patient_name LIKE %s)
                       ORDER BY id DESC LIMIT 20""",
                    (like, like)
                )
            else:
                cursor.execute(
                    """SELECT queue_number, patient_name, queue_type 
                       FROM hospital_queues 
                       WHERE DATE(created_at) = CURDATE() AND is_cancelled = 0
                       ORDER BY id DESC LIMIT 20"""
                )
            rows = cursor.fetchall()
            return {"status": "success", "data": rows}
    finally:
        connection.close()

@app.get("/api/history")
def get_queue_history(target_date: Optional[str] = None):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            query = f"""
                SELECT 
                    q.id, q.queue_number, q.patient_name, q.queue_type,
                    q.created_at, qs.station_name, qs.status, qs.updated_at
                FROM hospital_queues q
                LEFT JOIN queue_stations qs ON q.id = qs.queue_id
                WHERE DATE(q.created_at) = { "%s" if target_date else "CURDATE()" }
                ORDER BY q.id DESC, qs.id ASC
            """
            if target_date:
                cursor.execute(query, (target_date,))
            else:
                cursor.execute(query)
                
            rows = cursor.fetchall()
            return {"status": "success", "data": rows}
    finally:
        connection.close()

@app.get("/{file_path:path}")
async def serve_static_files(file_path: str):
    if not file_path or file_path == "/":
        file_path = "index.html"
    
    if os.path.exists(file_path) and os.path.isfile(file_path):
        media_type, _ = mimetypes.guess_type(file_path)
        return FileResponse(file_path, media_type=media_type)
    
    if not os.path.extsep in file_path and os.path.exists(f"{file_path}.html"):
        return FileResponse(f"{file_path}.html", media_type="text/html")

    # 🐛 แก้บั๊ก: เดิมถ้าไฟล์ไหนก็ตามหาไม่เจอ (แม้จะมีนามสกุลไฟล์ชัดเจน เช่น .mp3, .png)
    # จะ fallback ไปเสิร์ฟ index.html แทนแบบเงียบๆ (status 200 OK) ทำให้ฝั่งหน้าเว็บเข้าใจผิดว่าโหลดไฟล์สำเร็จ
    # เช่น fetch() ไฟล์เสียงที่ยังไม่ได้สร้าง (letter_M.mp3) จะได้ index.html กลับมาแทนโดยไม่รู้ตัว
    # แล้วเอาไปพยายามถอดรหัสเป็นเสียงต่อ ทำให้ระบบเสียงพังแบบหาสาเหตุยาก
    # ตอนนี้ถ้ามีนามสกุลไฟล์ชัดเจนแต่หาไฟล์นั้นไม่เจอ จะตอบ 404 จริงๆ แทน ไม่ fallback ไป index.html
    if os.path.extsep in file_path:
        raise HTTPException(status_code=404, detail=f"ไม่พบไฟล์ {file_path}")

    if os.path.exists("index.html"):
        return FileResponse("index.html", media_type="text/html")
        
    raise HTTPException(status_code=404, detail="File Not Found")