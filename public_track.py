"""
public_track.py
================
🔓 บริการ "เช็คสถานะคิว" แบบสาธารณะ (Public) — ตัวเดียวที่ควรเปิดออกอินเทอร์เน็ต

ทำไมต้องแยกไฟล์นี้ออกจาก main.py:
- main.py มี endpoint ที่ "เขียน/แก้ไข" ข้อมูลได้ (ลงทะเบียน, เรียกคิว, ปิดคิว, แก้โปรแกรมตรวจ ฯลฯ)
  ถ้าเปิด main.py ทั้งไฟล์ออกอินเทอร์เน็ต ใครก็ตามที่เจอลิงก์ (แม้จะเดายาก) จะสามารถยิง API
  ไปเรียกคิว/ปิดคิว/ดูรายชื่อคนไข้ทั้งวันได้ทันที — อันตรายมากสำหรับระบบโรงพยาบาล
- ไฟล์นี้จึงมีแค่ endpoint แบบ "อ่านอย่างเดียว" (GET) และต้องรู้เลขคิวที่แน่นอนก่อนถึงจะดูได้
  ปลอดภัยพอที่จะเปิดให้คนไข้เข้าถึงจากมือถือ/เน็ตนอกได้

วิธีรัน (บนเครื่อง Server เดียวกับ main.py แต่คนละพอร์ต):
    uvicorn public_track:app --host 0.0.0.0 --port 8001

จากนั้นค่อยเปิด "เฉพาะพอร์ต 8001" นี้ออกอินเทอร์เน็ต (ผ่าน Cloudflare Tunnel / Tailscale Funnel)
ส่วน main.py (พอร์ต 8000) ให้เข้าถึงได้เฉพาะใน LAN โรงพยาบาลเท่านั้นเหมือนเดิม
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import pymysql
import json
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    # 🔒 อนุญาต POST เพิ่มเข้ามาแล้ว เฉพาะสำหรับ /api/book-appointment เท่านั้น (ดูรายละเอียดที่ตัวฟังก์ชัน)
    # ความปลอดภัยตอนนี้อยู่ที่ DB user "public_booking" ที่เขียนได้แค่ตาราง general_appointments ตารางเดียว
    # ไม่ใช่การบล็อก HTTP method แล้ว (เพราะฟีเจอร์จองคิวจำเป็นต้องมี POST)
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ตั้งค่าเดียวกับ main.py — ถ้ารันคนละเครื่องกับ MySQL ให้แก้ host เป็น IP ของเครื่อง DB
# ⚙️ ตั้งค่าการเชื่อมต่อฐานข้อมูล — เครื่องนี้รันแยกจากเครื่อง main.py/MySQL
# ต้องแก้ host เป็น IP วง LAN ของเครื่อง Server จริง (ไม่ใช่ localhost แล้ว)
# แนะนำให้ใช้ user แยกที่มีสิทธิ์แค่ SELECT เท่านั้น (ดูขั้นตอนสร้าง user ใน README/คำแนะนำที่แชทให้ไว้)
DB_HOST = "10.121.111.233"          # 👈 IP วง LAN ของเครื่อง MySQL Server จริง
DB_USER = "root"           # 👈 user ที่สร้างไว้เฉพาะ (สิทธิ์ SELECT อย่างเดียว)
DB_PASSWORD = "@Nurak@1234"    # 👈 รหัสผ่านของ track_readonly ที่ตั้งไว้ตอนสร้าง user (ห้ามมีภาษาไทย/อักขระพิเศษ)
DB_NAME = "hospital_queue_db"

# 🛡️ เช็คตั้งแต่ตอนรัน ถ้ายังไม่ได้แก้ค่า placeholder ด้านบน ให้หยุดทันทีพร้อมบอกชัดๆ
# กันไม่ให้เจอ UnicodeEncodeError (latin-1) ที่งงว่าเกิดจากอะไร ตอนพยายามเชื่อมต่อ MySQL จริง
if DB_PASSWORD == "ตั้งรหัสผ่านที่นี่" or not DB_PASSWORD.isascii():
    raise SystemExit(
        "❌ ยังไม่ได้ตั้งค่า DB_PASSWORD ให้เป็นรหัสผ่านจริง (หรือมีอักขระที่ไม่ใช่ภาษาอังกฤษ/ตัวเลขปนอยู่)\n"
        "   ไปแก้ตัวแปร DB_PASSWORD ในไฟล์นี้ก่อนรัน ใช้รหัสผ่านภาษาอังกฤษ/ตัวเลขเท่านั้น (ห้ามมีภาษาไทย)\n"
        "   ถ้ายังไม่เคยสร้าง user track_readonly ให้รันคำสั่งนี้ใน MySQL ก่อน:\n"
        "   CREATE USER 'track_readonly'@'%' IDENTIFIED BY 'YourPasswordHere';\n"
        "   GRANT SELECT ON hospital_queue_db.* TO 'track_readonly'@'%';"
    )

def get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5   # กันเครื่อง track ค้างนานเกินไปถ้า DB Server เข้าไม่ถึง (เช่น เน็ต LAN หลุด)
    )

# ==========================================================
# 📅 ระบบจองคิวออนไลน์ทั่วไป — คนไข้กรอกเองจากมือถือผ่าน QR Code
# ⚠️ ใช้ DB user แยกต่างหาก (public_booking) ที่มีสิทธิ์ INSERT ได้แค่ตาราง general_appointments
# ตารางเดียวเท่านั้น — ไม่ใช้ DB_USER/DB_PASSWORD ตัวเดียวกับด้านบน (track_readonly) เด็ดขาด
# เพราะจุดนี้ต้อง "เขียน" ข้อมูลได้ ถ้าใช้ user เดียวกับด้านบนจะทำลายหลักการ read-only ทั้งไฟล์นี้
# ==========================================================
BOOKING_DB_USER = "public_booking"           # 👈 user ที่สร้างไว้เฉพาะ (สิทธิ์ INSERT ได้แค่ general_appointments)
BOOKING_DB_PASSWORD = "ตั้งรหัสผ่านที่นี่ให้ตรงกับตอนสร้าง user"  # 👈 ต้องตรงกับรหัสผ่านที่ตั้งไว้ตอนรัน migration

def get_booking_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=BOOKING_DB_USER,
        password=BOOKING_DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5
    )

GENERAL_TEST_STATIONS = ['Blood', 'Eye', 'Ear', 'Lung', 'EKG', 'XRay', 'Ultrasound', 'Mammogram']

STATION_LABELS = {
    'VitalSign': 'จุดคัดกรอง (Vital Sign)',
    'Blood': 'ห้องเจาะเลือด',
    'Eye': 'ห้องตรวจตา',
    'Ear': 'ห้องตรวจหู',
    'Lung': 'ห้องตรวจปอด',
    'EKG': 'ห้อง EKG',
    'XRay': 'ห้องเอกซเรย์ (X-Ray ปอด)',
    'Ultrasound': 'ห้องอัลตราซาวด์ (Ultrasound)',
    'Mammogram': 'ห้องแมมโมแกรม (Mammogram)',
    'Doctor': 'ห้องตรวจแพทย์',
}
for _i in range(1, 9):
    STATION_LABELS[f'Doctor_{_i}'] = f'ห้องตรวจแพทย์ {_i}'

FOCUS_MESSAGE = {
    'Calling': "🔴 กำลังเรียกคิวของคุณอยู่ที่ {label} กรุณาไปที่จุดบริการทันที",
    'Pending': "🟢 คิวของคุณกำลังรออยู่ที่ {label} กรุณารอฟังเรียกคิว",
    'Hold':    "⏸️ คิวของคุณถูกพักไว้ชั่วคราวที่ {label} กรุณาติดต่อเจ้าหน้าที่จุดนั้น",
}
# ✅ ยกเลิกจุดการเงิน (Cashier) ออกจากระบบแล้ว — ทุกจุดหลัง VitalSign (ทั้งจุดตรวจทั่วไปและห้องหมอ)
# ต้องยื่นบัตรคิว/สแกน CN กับพยาบาลที่จุดนั้นก่อนเสมอ ไม่มีจุดไหนปลดล็อกอัตโนมัติแล้ว
LOCK_MESSAGE_GENERAL = "📍 กรุณายื่นบัตรคิวกับพยาบาลที่ {label} เพื่อเข้าคิว"

# 🌟 จุดที่มีหลายห้องย่อยคู่ขนาน — ต้องรู้จักไว้เหมือนกับ main.py เพราะพอถูกเรียกคิว ชื่อแถวในฐานข้อมูล
# จะถูกเปลี่ยนจาก canonical (เช่น "Blood") เป็นห้องเฉพาะ (เช่น "Blood_2") แล้วค้างชื่อนั้นไว้ตอน Completed ด้วย
# ถ้า query แค่ชื่อ canonical เฉยๆ จะหาสถานะไม่เจอเลยหลังถูกเรียกคิว (เห็นเป็น "ยังไม่จองคิว" ทั้งที่เสร็จแล้วจริง)
MULTI_ROOM_STATIONS = {
    'Blood': [f'Blood_{i}' for i in range(1, 5)],
    'Eye': [f'Eye_{i}' for i in range(1, 5)],
    'Ear': [f'Ear_{i}' for i in range(1, 4)],
    'EKG': [f'EKG_{i}' for i in range(1, 3)],
    'Lung': [f'Lung_{i}' for i in range(1, 3)],
    'Doctor': [f'Doctor_{i}' for i in [1, 2, 3, 4, 5, 6, 8]],
}

# ลำดับความสำคัญของสถานะ — ถ้าเจอหลายแถวที่เกี่ยวข้องกัน (เช่น Blood ตัวเองยัง Lock อยู่ แต่ Blood_2 เป็น Completed)
# ให้ใช้ตัวที่ "ก้าวหน้าที่สุด" เป็นตัวแทนสถานะจริงของจุดนั้น
STATUS_PRIORITY = ['Completed', 'Calling', 'Hold', 'Pending', 'Lock', 'Skipped']

def resolve_canonical_status(canonical_name, all_rows):
    """หาสถานะจริงของจุด canonical (เช่น 'Blood') จากทุกแถวที่เกี่ยวข้อง (ทั้งชื่อ canonical เองและห้องย่อยทั้งหมด)"""
    candidates = [canonical_name] + MULTI_ROOM_STATIONS.get(canonical_name, [])
    found_statuses = [all_rows[name] for name in candidates if name in all_rows]
    if not found_statuses:
        return "Lock"
    for priority_status in STATUS_PRIORITY:
        if priority_status in found_statuses:
            return priority_status
    return found_statuses[0]

def get_waiting_ahead_count(cursor, canonical_station, this_queue_id):
    """
    นับจำนวนคนที่ "รอก่อน" คิวนี้ที่จุดตรวจเดียวกัน (นับเฉพาะ Pending) — ใช้สูตรเรียงลำดับเดียวกับ main.py
    (DATE_SUB(qs.updated_at, INTERVAL (created_at - priority_time) SECOND)) กันนับผิดพลาดถ้ามีคิวแบบนัด X-Ray ปนอยู่
    """
    candidates = [canonical_station] + MULTI_ROOM_STATIONS.get(canonical_station, [])

    cursor.execute("""
        SELECT DATE_SUB(qs.updated_at, INTERVAL TIMESTAMPDIFF(SECOND, q.priority_time, q.created_at) SECOND) as eff_time
        FROM queue_stations qs
        JOIN hospital_queues q ON qs.queue_id = q.id
        WHERE qs.queue_id = %s AND qs.station_name IN %s AND qs.status = 'Pending'
        LIMIT 1
    """, (this_queue_id, tuple(candidates)))
    my_row = cursor.fetchone()
    if not my_row:
        return None  # ไม่ได้อยู่ในสถานะ Pending ที่จุดนี้ (เช่น ยังไม่สแกนเข้าคิว หรือกำลังตรวจ/เสร็จแล้ว) ไม่ต้องนับ
    my_eff_time = my_row['eff_time']

    cursor.execute("""
        SELECT COUNT(*) as cnt
        FROM queue_stations qs
        JOIN hospital_queues q ON qs.queue_id = q.id
        WHERE qs.station_name IN %s
          AND qs.status = 'Pending'
          AND DATE(q.created_at) = CURDATE()
          AND qs.queue_id != %s
          AND DATE_SUB(qs.updated_at, INTERVAL TIMESTAMPDIFF(SECOND, q.priority_time, q.created_at) SECOND) < %s
    """, (tuple(candidates), this_queue_id, my_eff_time))
    return cursor.fetchone()['cnt']


def mask_name(name: str) -> str:
    """แสดงชื่อแบบปิดบังบางส่วน เพื่อความเป็นส่วนตัวบนหน้าสาธารณะ เช่น 'สมชาย ใจดี' -> 'สมชาย จ.'"""
    if not name:
        return "-"
    parts = name.strip().split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    rest_initials = " ".join(f"{p[0]}." for p in parts[1:] if p)
    return f"{first} {rest_initials}".strip()


@app.get("/api/health")
def health():
    return {"status": "running", "service": "public_track"}


@app.get("/api/track/{queue_number}")
def track_queue(queue_number: str):
    try:
        connection = get_db_connection()
    except Exception as e:
        # เครื่องนี้เชื่อม MySQL ข้ามเครือข่าย (ผ่าน LAN โรงพยาบาล) ถ้าสายหลุด/สัญญาณขาด จะเจอ error ตรงนี้
        print(f"❌ [DB connection error] {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="ระบบเช็คสถานะขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งในอีกสักครู่")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, queue_number, patient_name, queue_type, stations, "
                "DATE_FORMAT(created_at, '%%d/%%m/%%Y %%H:%%i:%%s') as created_at_str "
                "FROM hospital_queues WHERE queue_number = %s AND DATE(created_at) = CURDATE()",
                (queue_number,)
            )
            q_res = cursor.fetchone()
            if not q_res:
                raise HTTPException(status_code=404, detail="ไม่พบคิวนี้ในระบบวันนี้ (อาจพิมพ์เลขคิวผิด หรือเลยเวลาให้บริการแล้ว)")

            cursor.execute(
                "SELECT station_name, status FROM queue_stations WHERE queue_id = %s",
                (q_res['id'],)
            )
            status_rows = {r['station_name']: r['status'] for r in cursor.fetchall()}

            planned = json.loads(q_res['stations']) if q_res['stations'] else []

            stations_status = []
            for st in planned:
                station_status = resolve_canonical_status(st, status_rows)
                entry = {
                    "station": st,
                    "label": STATION_LABELS.get(st, st),
                    "status": station_status,
                }
                if station_status == "Pending":
                    entry["waiting_ahead"] = get_waiting_ahead_count(cursor, st, q_res['id'])
                stations_status.append(entry)

            # หาจุดที่ "กำลังดำเนินการอยู่ตอนนี้" (จุดแรกในลำดับที่ยังไม่ Completed/Skipped)
            focus = None
            for s in stations_status:
                if s["status"] not in ("Completed", "Skipped"):
                    focus = s
                    break

            if focus:
                if focus["status"] == "Lock":
                    # ทุกจุดหลังลงทะเบียน (ทั้งจุดตรวจทั่วไปและห้องหมอ) ต้องยื่นบัตรคิว/สแกน CN กับพยาบาลก่อนเสมอ
                    focus_message = LOCK_MESSAGE_GENERAL.format(label=focus["label"])
                else:
                    focus_message = FOCUS_MESSAGE.get(focus["status"], "กรุณารอการอัปเดตสถานะ").format(label=focus["label"])
            elif len(planned) == 0:
                # 🌟 ยังไม่มีแผนตรวจเลย (ลงทะเบียนเสร็จแต่ยังไม่ได้ซักประวัติ/กำหนดโปรแกรม) — ต่างจาก "เสร็จสิ้นแล้ว"
                focus_message = "📋 กรุณาติดต่อเคาน์เตอร์พยาบาลเพื่อซักประวัติและกำหนดโปรแกรมตรวจ"
            else:
                focus_message = "✅ คุณเสร็จสิ้นขั้นตอนการรับบริการทั้งหมดแล้ว ขอบคุณที่ใช้บริการค่ะ"

            return {
                "status": "success",
                "queue_number": q_res['queue_number'],
                "patient_name": mask_name(q_res['patient_name']),
                "queue_type": q_res['queue_type'],
                "created_at": q_res['created_at_str'],
                "stations_status": stations_status,
                "focus_message": focus_message,
                "is_done": focus is None and len(planned) > 0
            }
    finally:
        connection.close()


# ==========================================================
# 📅 API รับข้อมูลจองคิวจากฟอร์มสาธารณะ (POST เดียวที่มีในไฟล์นี้ทั้งหมด — เขียนได้แค่ตารางนี้ตารางเดียว)
# ==========================================================
from fastapi import Body
from datetime import date as date_cls

@app.post("/api/book-appointment")
async def book_appointment(payload: dict = Body(...)):
    patient_name = (payload.get("patient_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    appointment_date = (payload.get("appointment_date") or "").strip()
    appointment_time = (payload.get("appointment_time") or "").strip() or None
    cn = (payload.get("cn") or "").strip() or None
    note = (payload.get("note") or "").strip()[:255] or None

    # ตรวจสอบข้อมูลเบื้องต้น — กันข้อมูลมั่วๆ ก่อนเข้าฐานข้อมูล (เพราะ endpoint นี้เปิดสาธารณะ ไม่มี login)
    if not patient_name or len(patient_name) < 2:
        raise HTTPException(status_code=400, detail="กรุณากรอกชื่อ-สกุลให้ถูกต้อง")
    if not phone or len(phone) < 9 or not phone.replace("-", "").isdigit():
        raise HTTPException(status_code=400, detail="กรุณากรอกเบอร์โทรศัพท์ให้ถูกต้อง")
    try:
        parsed_date = date_cls.fromisoformat(appointment_date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="กรุณาเลือกวันที่ให้ถูกต้อง")
    if parsed_date < date_cls.today():
        raise HTTPException(status_code=400, detail="ไม่สามารถจองวันที่ผ่านไปแล้วได้")

    connection = get_booking_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO general_appointments
                   (appointment_date, appointment_time, patient_name, phone, cn, note, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'scheduled')""",
                (appointment_date, appointment_time, patient_name, phone, cn, note)
            )
            connection.commit()
    finally:
        connection.close()

    return {"status": "success", "message": "บันทึกการจองเรียบร้อยแล้ว กรุณามาตามวันที่นัดไว้ พร้อมแจ้งชื่อ-เบอร์โทรที่จองไว้กับพนักงาน"}

# เสิร์ฟไฟล์ static เฉพาะ book.html เท่านั้น (ฟอร์มจองคิวสาธารณะ)
@app.get("/book.html")
async def serve_book_page():
    if os.path.exists("book.html"):
        return FileResponse("book.html", media_type="text/html")
    raise HTTPException(status_code=404, detail="ไม่พบไฟล์ book.html")


# เสิร์ฟไฟล์ static เฉพาะ track.html เท่านั้น (ไม่เปิดไฟล์อื่นในเครื่องออกสาธารณะ)
@app.get("/track.html")
async def serve_track_page():
    if os.path.exists("track.html"):
        return FileResponse("track.html", media_type="text/html")
    raise HTTPException(status_code=404, detail="ไม่พบไฟล์ track.html")


@app.get("/")
async def root_redirect():
    if os.path.exists("track.html"):
        return FileResponse("track.html", media_type="text/html")
    raise HTTPException(status_code=404, detail="ไม่พบไฟล์ track.html")