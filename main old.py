from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel
from typing import List, Optional
import pymysql
import json
import os
import mimetypes
import uuid
import re
import io

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
        host="localhost",
        user="root",
        password="@Nurak@1234",
        database="hospital_queue_db",
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

DOCTOR_STATIONS = [f"Doctor_{i}" for i in range(1, 9)]
STATIONS_LIST = ['VitalSign', 'Blood', 'Eye', 'Ear', 'Lung', 'EKG', 'XRay', 'Doctor', 'Cashier'] + DOCTOR_STATIONS

# สถานีตรวจ "ทั่วไป" ที่ต้องรอการกำหนดโปรแกรมตรวจ (Program Assignment) จากจุด VitalSign ก่อน
# ถึงจะถูกปลดล็อกเป็น Pending — ป้องกันการเดินข้ามขั้นตอนไปสุ่มทำจุดใดจุดหนึ่งก่อนได้ตามใจชอบ
GENERAL_TEST_STATIONS = ['Blood', 'Eye', 'Ear', 'Lung', 'EKG', 'XRay']

STATION_MAP = {
    'ตรวจวัดสัญญาณชีพ': 'VitalSign',
    'เจาะเลือด': 'Blood',
    'ตรวจตา': 'Eye',
    'ตรวจหู': 'Ear',
    'ตรวจปอด': 'Lung',
    'ห้องตรวจคลื่นไฟฟ้าหัวใจ': 'EKG',
    'คลื่นไฟฟ้าหัวใจ': 'EKG',
    'เอกซเรย์': 'XRay',
    'X-Ray': 'XRay',
    'ห้องตรวจแพทย์': 'Doctor',
    'การเงินและรับยา': 'Cashier'
}

def normalize_station_name(name: str) -> str:
    if not name:
        return ""
    clean_name = name.strip()
    return STATION_MAP.get(clean_name, clean_name)

STATION_LABELS = {
    'VitalSign': 'จุดคัดกรอง (Vital Sign)',
    'Blood': 'ห้องเจาะเลือด',
    'Eye': 'ห้องตรวจตา',
    'Ear': 'ห้องตรวจหู',
    'Lung': 'ห้องตรวจปอด',
    'EKG': 'ห้อง EKG',
    'XRay': 'ห้องเอกซเรย์ (X-Ray)',
    'Doctor': 'ห้องตรวจแพทย์',
    'Cashier': 'การเงิน / รับยา',
}
for _i in range(1, 9):
    STATION_LABELS[f'Doctor_{_i}'] = f'ห้องตรวจแพทย์ {_i}'

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
                    GROUP BY q.queue_number, q.patient_name, q.queue_type, qs.status, qs.updated_at
                    ORDER BY 
                        CASE WHEN qs.status = 'Hold' THEN 0 ELSE 1 END ASC,
                        MIN(qs.id) ASC
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

                cursor.execute("""
                    SELECT q.queue_number, q.patient_name as name, q.queue_type, DATE_FORMAT(MAX(qs.updated_at), '%%H:%%i:%%s') as updated_at
                    FROM queue_stations qs
                    JOIN hospital_queues q ON qs.queue_id = q.id
                    WHERE LOWER(qs.station_name) = LOWER(%s) AND qs.status = 'Completed' AND DATE(q.created_at) = CURDATE()
                    GROUP BY q.queue_number, q.patient_name, q.queue_type
                    ORDER BY MAX(qs.updated_at) DESC LIMIT 10
                """, (st,))
                history_by_station[st] = cursor.fetchall()

            return {
                "current_calls": current_calls,
                "current_calls_names": current_calls_names,
                "waiting_patients": waiting_by_station,
                "history_patients": history_by_station,
                "station_labels": STATION_LABELS
            }
    finally:
        connection.close()

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
    selected_stations: List[str]
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

@app.get("/api/health")
def read_root():
    return {"status": "running"}

# 🔊 เสียงที่ใช้สร้างประกาศเรียกคิว — ใช้ pyttsx3 ดึงเสียงจาก Windows (SAPI5) โดยตรง
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
async def patient_add(payload: PatientAddModel):
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
async def register_patient(payload: RegisterPatientModel):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            prefix = "W" if payload.queue_type == "walkin" else "A"
            cursor.execute(
                "SELECT COUNT(*) as total FROM hospital_queues WHERE queue_type = %s AND DATE(created_at) = CURDATE()",
                (payload.queue_type,)
            )
            result = cursor.fetchone()
            queue_count = result['total'] + 1
            new_queue_num = f"{prefix}{queue_count:03d}"

            # กรองเฉพาะสถานีที่มีในระบบ (รายการนี้ถือเป็น "แผนตรวจเบื้องต้น" ที่หน้าลงทะเบียนแนะนำไว้เท่านั้น
            # แผนจริงจะถูกยืนยัน/ปรับอีกครั้งที่จุด VitalSign ผ่าน /api/assign-program)
            target_stations = [st for st in payload.selected_stations if st in STATIONS_LIST]

            # บังคับ VitalSign เสมอ (ต้องเป็นจุดแรกของทุกคน) และ Cashier เสมอ (จุดสุดท้าย)
            if 'VitalSign' not in target_stations:
                target_stations.insert(0, 'VitalSign')
            if 'Cashier' not in target_stations:
                target_stations.append('Cashier')

            first_station = 'VitalSign'

            cn_norm = normalize_cn(payload.cn) if payload.cn else None

            cursor.execute(
                "INSERT INTO hospital_queues "
                "(queue_number, patient_name, current_station, queue_type, stations, cn, hn, birth_date, gender) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (new_queue_num, payload.patient_name, first_station, payload.queue_type,
                 json.dumps(target_stations), cn_norm, payload.hn, payload.birth_date, payload.gender)
            )
            queue_id = cursor.lastrowid

            # 🌟 กติกาใหม่: ปลดล็อก (Pending) ให้เฉพาะ VitalSign เท่านั้นตอนลงทะเบียน
            # ส่วนจุดตรวจทั่วไปอื่นๆ (Blood/Eye/Ear/Lung/EKG/XRay) และ Doctor/Cashier จะเป็น 'Lock' ไว้ก่อน
            # แล้วจึงถูกปลดล็อกเฉพาะจุดที่จำเป็นจริง โดยพยาบาลที่จุด VitalSign ผ่าน /api/assign-program
            # เพื่อป้องกันไม่ให้คนไข้เดินสลับไปทำจุดใดก่อนก็ได้ตามใจตัวเองก่อนถูกคัดกรอง
            for station in target_stations:
                status = 'Pending' if station == 'VitalSign' else 'Lock'
                cursor.execute(
                    "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, %s)",
                    (queue_id, station, status)
                )
            
            # สร้างสเตชั่น Doctor_1 - Doctor_8 ไว้รองรับถ้ามีการเลือก Doctor
            if 'Doctor' in target_stations:
                for doc_st in DOCTOR_STATIONS:
                    cursor.execute(
                        "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, 'Lock')",
                        (queue_id, doc_st)
                    )

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

@app.get("/api/queue-plan/{queue_number}")
def get_queue_plan(queue_number: str):
    """ดึงแผนตรวจเบื้องต้นที่ลงทะเบียนไว้ ใช้สำหรับ prefill checkbox ตอนกำหนดโปรแกรมตรวจที่จุด VitalSign"""
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
                "SELECT id, queue_number, patient_name, queue_type, stations, DATE_FORMAT(created_at, '%%d/%%m/%%Y %%H:%%i:%%s') as created_at_str FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()",
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
            for st in ['VitalSign'] + GENERAL_TEST_STATIONS + ['Doctor', 'Cashier']:
                stations_status.append({
                    "station": st,
                    "label": STATION_LABELS.get(st, st),
                    "status": rows.get(st, "Lock")
                })

            return {
                "status": "success",
                "queue_number": q_res['queue_number'],
                "patient_name": q_res['patient_name'],
                "queue_type": q_res['queue_type'],
                "created_at": q_res['created_at_str'],
                "planned_stations": json.loads(q_res['stations']) if q_res['stations'] else [],
                "stations_status": stations_status
            }
    finally:
        connection.close()

@app.post("/api/assign-program")
async def assign_program(payload: AssignProgramModel):
    """
    🌟 ขั้นตอน "กำหนดโปรแกรมตรวจ" — เรียกใช้ที่จุด VitalSign หลังคัดกรองเสร็จ
    เพื่อยืนยัน/ปรับว่าคนไข้รายนี้ต้องเข้าจุดตรวจทั่วไปจุดไหนบ้างจริงๆ
    - จุดที่เลือก -> ปลดล็อกเป็น 'Pending' (ให้เดินไปจุดไหนก่อนก็ได้ตามใจ หลังจากนี้)
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
            IN_PROGRESS_OR_DONE = ('Calling', 'Completed')

            final_general_stations = []  # จุดที่ถือว่า "ยังอยู่ในกระบวนการ" จริงหลังแก้ไขครั้งนี้ (ใช้บันทึกลง stations JSON)

            for station in GENERAL_TEST_STATIONS:
                cur = current_status.get(station)
                if station in chosen:
                    # ต้องการให้ตรวจจุดนี้ -> ปลดล็อกเป็น Pending ถ้ายังไม่เคยเริ่ม/เคย Skip ไว้
                    if cur in LOCKED_OR_UNSTARTED:
                        cursor.execute(
                            "UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND station_name = %s",
                            (queue_id, station)
                        )
                        if cursor.rowcount == 0:
                            cursor.execute(
                                "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, 'Pending')",
                                (queue_id, station)
                            )
                    # ถ้ากำลังตรวจอยู่ (Calling) หรือเสร็จแล้ว (Completed) หรือ Pending/Hold อยู่แล้ว -> ไม่ต้องแตะ
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

            final_stations = ['VitalSign'] + final_general_stations + (['Doctor'] if payload.include_doctor else []) + ['Cashier']
            cursor.execute(
                "UPDATE hospital_queues SET stations = %s WHERE id = %s",
                (json.dumps(final_stations), queue_id)
            )

            # 🌟 สำคัญ: ถ้าต้องพบแพทย์ ต้องมั่นใจว่าแถว Doctor / Doctor_1-8 ถูกสร้างไว้แล้ว
            # เพราะตอนลงทะเบียนตอนนี้ Doctor ไม่ได้ถูกติ๊กไว้ default (สร้างแถวเฉพาะตอนเลือกไว้ตั้งแต่แรกเท่านั้น)
            # ถ้าไม่เช็คตรงนี้ กรณีพยาบาลมาเพิ่ม "ต้องพบแพทย์" ทีหลังจะไม่มีแถวให้ปลดล็อก คนไข้จะค้างไปต่อไม่ได้
            if payload.include_doctor:
                cursor.execute(
                    "SELECT COUNT(*) as cnt FROM queue_stations WHERE queue_id = %s AND LOWER(station_name) = 'doctor'",
                    (queue_id,)
                )
                if cursor.fetchone()['cnt'] == 0:
                    cursor.execute(
                        "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, 'Doctor', 'Lock')",
                        (queue_id,)
                    )
                for doc_st in DOCTOR_STATIONS:
                    cursor.execute(
                        "SELECT COUNT(*) as cnt FROM queue_stations WHERE queue_id = %s AND station_name = %s",
                        (queue_id, doc_st)
                    )
                    if cursor.fetchone()['cnt'] == 0:
                        cursor.execute(
                            "INSERT INTO queue_stations (queue_id, station_name, status) VALUES (%s, %s, 'Lock')",
                            (queue_id, doc_st)
                        )

            # ถ้าไม่มีจุดตรวจทั่วไปที่ต้องทำต่อเลย (ทุกจุดตรวจเสร็จ/ถูกยกเลิกหมดแล้ว) ให้ปลดล็อกขั้นตอนถัดไปทันที
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM queue_stations WHERE queue_id = %s AND station_name IN %s AND status NOT IN ('Completed', 'Skipped')",
                (queue_id, tuple(GENERAL_TEST_STATIONS))
            )
            remaining = cursor.fetchone()['cnt']
            if remaining == 0:
                if payload.include_doctor:
                    cursor.execute(
                        "UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND (LOWER(station_name) = 'doctor' OR LOWER(station_name) LIKE 'doctor_%%') AND status = 'Lock'",
                        (queue_id,)
                    )
                else:
                    cursor.execute(
                        "UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND LOWER(station_name) = 'cashier' AND status = 'Lock'",
                        (queue_id,)
                    )

            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success", "assigned_stations": final_general_stations}

@app.post("/api/call-queue")
async def call_queue(payload: CallQueueModel):
    target_station = normalize_station_name(payload.station_name)
    
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
async def recall_queue(payload: CallQueueModel):
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
async def hold_queue(payload: CallQueueModel):
    target_station = normalize_station_name(payload.station_name)
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE queue_stations qs
                JOIN hospital_queues q ON qs.queue_id = q.id
                SET qs.status = 'Hold'
                WHERE q.queue_number = %s AND LOWER(qs.station_name) = LOWER(%s) AND DATE(q.created_at) = CURDATE()
            """, (payload.queue_number, target_station))
            connection.commit()
    finally:
        connection.close()

    updated_data = fetch_queue_data()
    await manager.broadcast(json.dumps(updated_data))
    return {"status": "success"}

@app.post("/api/finish-queue")
async def finish_queue(payload: CallQueueModel):
    target_station = normalize_station_name(payload.station_name)
    
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

                # กรณีจุดที่เพิ่งตรวจเสร็จเป็นสถานีตรวจทั่วไป (VitalSign, Blood, Eye, Ear, Lung, EKG, XRay)
                if st_name in ['vitalsign', 'blood', 'eye', 'ear', 'lung', 'ekg', 'xray']:
                    # ตรวจสอบว่ามีสถานีตรวจทั่วไปอื่นๆ ในรายการที่ยังตรวจไม่เสร็จ (ยังไม่เป็น Completed) หรือไม่
                    cursor.execute("""
                        SELECT COUNT(*) as uncompleted_count 
                        FROM queue_stations 
                        WHERE queue_id = %s 
                          AND LOWER(station_name) IN ('vitalsign', 'blood', 'eye', 'ear', 'lung', 'ekg', 'xray')
                          AND LOWER(station_name) IN %s
                          AND status NOT IN ('Completed', 'Skipped')
                    """, (queue_id, tuple(assigned_lower)))
                    uncompleted_res = cursor.fetchone()
                    uncompleted_count = uncompleted_res['uncompleted_count'] if uncompleted_res else 0

                    # ถ้าตรวจสถานีทั่วไปหมดทุกจุดแล้ว จึงจะปลดล็อกขั้นตอนถัดไป (Doctor หรือ Cashier)
                    if uncompleted_count == 0:
                        if "doctor" in assigned_lower:
                            cursor.execute(
                                "UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND (LOWER(station_name) = 'doctor' OR LOWER(station_name) LIKE 'doctor_%%')", 
                                (queue_id,)
                            )
                        else:
                            cursor.execute("UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND LOWER(station_name) = 'cashier'", (queue_id,))
                
                # กรณีจุดที่เพิ่งตรวจเสร็จเป็นห้องตรวจแพทย์ (Doctor หรือ Doctor_1 - Doctor_8)
                elif st_name == "doctor" or st_name.startswith("doctor_"):
                    cursor.execute("""
                        UPDATE queue_stations 
                        SET status = 'Completed' 
                        WHERE queue_id = %s AND (LOWER(station_name) = 'doctor' OR LOWER(station_name) LIKE 'doctor_%%')
                    """, (queue_id,))
                    
                    cursor.execute("UPDATE queue_stations SET status = 'Pending' WHERE queue_id = %s AND LOWER(station_name) = 'cashier'", (queue_id,))

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
                       WHERE DATE(created_at) = CURDATE() AND (queue_number LIKE %s OR patient_name LIKE %s)
                       ORDER BY id DESC LIMIT 20""",
                    (like, like)
                )
            else:
                cursor.execute(
                    """SELECT queue_number, patient_name, queue_type 
                       FROM hospital_queues 
                       WHERE DATE(created_at) = CURDATE() 
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

    if os.path.exists("index.html"):
        return FileResponse("index.html", media_type="text/html")
        
    raise HTTPException(status_code=404, detail="File Not Found")