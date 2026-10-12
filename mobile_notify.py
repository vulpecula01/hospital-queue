# -*- coding: utf-8 -*-
"""
mobile_notify.py
==================
สคริปต์แจ้งเตือนตารางออกหน่วย Mobile รายวัน (ฟรี ผ่าน Telegram บนมือถือ)

โฟกัสหลักของเวอร์ชันนี้: สรุป "ของที่ต้องเตรียม/ยืมจาก รพ. วันนี้" ให้ชัดเจนที่สุด

สิ่งที่สคริปต์นี้ทำ:
1. สแกนไฟล์ .xlsx ทุกไฟล์ในโฟลเดอร์ตารางออกหน่วย หาแถวของ "วันที่" เป้าหมาย
   (ค่าเริ่มต้น = พรุ่งนี้ ปรับได้ที่ DAYS_AHEAD)
2. สำหรับแต่ละงาน อ่านคอลัมน์สรุปรายชื่อ (เช่น "ตา+เจ้าหน้าที่รพ. = 4จ้าง4") แล้วนับ
   จำนวนเครื่อง/คนจริงจากชื่อ+จำนวนที่จ้าง (ไม่เชื่อตัวเลขนำหน้าเฉย ๆ)
3. เทียบกับคอลัมน์ละเอียดของแต่ละผู้รับเหมา (พี่แอ๋ว/คุณเอ้ CMR/Mynurz ฯลฯ) เพื่อดูว่า
   "จ้าง" แต่ละรายการนั้น ใช้เครื่องของโรงพยาบาลเอง หรือผู้รับจ้างเอาเครื่องมาเอง
4. รวมยอดทั้งวันเป็นเช็คลิสต์ "ของที่ต้องเตรียม/ยืมจาก รพ. วันนี้" ต่อประเภทงาน (หู/ตา/ปอด)
   พร้อมรายละเอียดต่องานด้านล่าง แล้วส่งเข้า Telegram

วิธีตั้งค่า (ครั้งเดียว):
1. สร้าง Telegram Bot ฟรี:
   - เปิดแชทกับ @BotFather ใน Telegram -> พิมพ์ /newbot -> ตั้งชื่อบอท
   - จะได้ "Bot Token" มา (รูปแบบ 123456789:ABC-defGhIjk...)
2. หา Chat ID ของกลุ่ม/ผู้รับ:
   - เพิ่มบอทเข้ากลุ่ม Telegram หรือคุยกับบอทโดยตรง ส่งข้อความอะไรก็ได้ 1 ข้อความ
   - เปิด https://api.telegram.org/bot<TOKEN>/getUpdates ในเบราว์เซอร์
   - จะเห็น "chat":{"id": ...} นั่นคือ Chat ID
3. แก้ค่าใน CONFIG ด้านล่างนี้ให้ตรงกับของจริง
4. ตั้ง Windows Task Scheduler (หรือ cron บน Linux) ให้รันสคริปต์นี้ทุกวัน — ดู README.md

Dependencies: pip install openpyxl requests --break-system-packages (ถ้ายังไม่มี)
"""

import glob
import os
import re
import logging
import datetime
from typing import Optional

import openpyxl

try:
    import requests
except ImportError:
    requests = None


# ============================== CONFIG ===================================
CONFIG = {
    # โฟลเดอร์ที่เก็บไฟล์ตารางเวร (แก้เป็น path จริงของคุณ)
    "SCHEDULE_FOLDER": r"\\10.121.10.33\FileShare\Health Promotion\ตารางเวร Mobile\ตารางออกหน่วย\ตารางออกหน่วยปี69",

    # จำนวนวันล่วงหน้าที่จะแจ้งเตือน (1 = แจ้งของพรุ่งนี้, 0 = แจ้งของวันนี้)
    "DAYS_AHEAD": 1,

    # --- ช่องทางแจ้งเตือน: Telegram (ฟรี, ใช้งานบนมือถือได้ทันที) ---
    "TELEGRAM_BOT_TOKEN": "ใส่_BOT_TOKEN_ตรงนี้",
    "TELEGRAM_CHAT_ID": "ใส่_CHAT_ID_ตรงนี้",

    "BUDDHIST_YEAR_OFFSET": 543,
    "REQUIRED_HEADERS": ["วัน", "บริษัท"],

    "MAIN_FIELDS": [
        "บริษัท", "Location", "ประเภท Service", "เวลา",
        "จำนวนผู้เข้ารับการตรวจสุขภาพ/วัน",
    ],

    # คอลัมน์ "อุปกรณ์/บริการหลัก" ที่ถูกมาร์ก P หรือมีค่าระบุ
    "EQUIPMENT_FIELDS": [
        "แพทย์", "ร้านแว่น", "ตรวจสายตาแผนกตา", "อาหารว่าง", "Incharge",
        "จุดเจาะเลือด", "ลงะเบียน", "Vital sin (นน.,สูง,รอบเอว,BP)", "ตรวจ EKG",
        "รถ.X-ray (chest)", "จนท.X-ray (chest)", "รถ Mamogram", "จนท.Mamogram",
        "รถ X-ray+U/S", "รถ Ultrasound", "จนท.Ultrasound",
        "จนท.จัดคิว X ray(ใครก็ได้)", "ตู้ตรวจการได้ยิน", "รถตรวจการได้ยิน",
        "จนท.ตรวจการได้ยิน", "เครื่องตรวจตา", "จนท.ตรวจตา", "เครื่องเป่าปอด",
        "จนท.เป่าปอด", "กายภาพ", "จนท.Lab", "Ambulance", "Responder",
        "Predictive", "จุดวัดความดัน", "จุดฉีดวัคซีน", "Occupation Eye",
    ],

    # จุด (ดวลชื่อ) ที่ต้อง "นับจำนวนเครื่อง/คนจากรายชื่อ" แทนการเชื่อตัวเลขนำหน้าเฉย ๆ
    # และเทียบกับคอลัมน์ผู้รับเหมาด้านล่างว่าใช้เครื่องของ รพ. หรือผู้รับจ้างเอามาเอง
    # key = ชื่อที่จะแสดงในรายงาน, summary_header = ชื่อคอลัมน์สรุปในไฟล์,
    # keywords = คำที่ใช้จับคู่กับคอลัมน์ละเอียดของผู้รับเหมา
    "DUTY_MAP": {
        "หู (ตรวจการได้ยิน)": {"summary_header": "หู+เจ้าหน้าที่รพ.", "keywords": ["หู"]},
        "ตา (Occ.Eye / VA)": {"summary_header": "ตา+เจ้าหน้าที่รพ.", "keywords": ["Occeye", "VA", "ตา"]},
        "เป่าปอด": {"summary_header": "เป่าปอด", "keywords": ["ปอด"]},
    },
}
# ===========================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def normalize_header(h) -> str:
    return "" if h is None else str(h).strip()


def find_header_row(ws, max_scan_rows: int = 6) -> Optional[int]:
    required = CONFIG["REQUIRED_HEADERS"]
    for r in range(1, max_scan_rows + 1):
        row_values = [normalize_header(ws.cell(row=r, column=c).value)
                      for c in range(1, min(ws.max_column, 15) + 1)]
        if all(any(req in val for val in row_values) for req in required):
            return r
    return None


def excel_date_to_gregorian(value) -> Optional[datetime.date]:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        dt = datetime.datetime(value.year, value.month, value.day)
    else:
        return None
    year = dt.year
    if year > 2400:
        year -= CONFIG["BUDDHIST_YEAR_OFFSET"]
    try:
        return datetime.date(year, dt.month, dt.day)
    except ValueError:
        return None


def classify_hire_source(header: str) -> str:
    """บอกว่าคอลัมน์ผู้รับเหมานี้ใช้เครื่องของโรงพยาบาล หรือผู้รับจ้างเอามาเอง

    "รถรพ" (รถของโรงพยาบาล) เป็นคำที่พูดถึง "พาหนะ" ไม่ใช่สัญญาณว่าเครื่องตรวจเป็นของ รพ.
    จึงตัดคำนี้ออกก่อน มิฉะนั้นหัวคอลัมน์ที่พูดถึงรถของ รพ. จะถูกเข้าใจผิดว่าเครื่องตรวจ
    เป็นของ รพ. ทั้งที่จริงคือผู้รับจ้างเอาเครื่องมาเอง.
    """
    cleaned = header.replace("รถรพ", "")
    has_rph = bool(re.search(r"รพ|ร\.พ", cleaned))
    has_sub = bool(re.search(r"sub", cleaned, re.I))
    if has_rph:
        return "hospital"
    if has_sub:
        return "contractor"
    return "unclear"


def count_named_units(value: str):
    """นับจำนวนหน่วย (คน/เครื่อง) จากข้อความ เช่น '2สุวิมล+จ้าง1' -> 3
    (สุวิมล = 1, จ้าง1 = 1 คนที่จ้างมา) แทนการเชื่อเลขนำหน้าอย่างเดียว
    คืนค่า: (จำนวนที่นับได้จริง, จำนวนที่จ้างทั้งหมด, จำนวนที่นำหน้า(ถ้ามี))
    """
    s = str(value).strip()
    m = re.match(r"^(\d+)", s)
    reported = int(m.group(1)) if m else None
    rest = s[m.end():] if m else s
    tokens = [t.strip() for t in rest.split("+") if t.strip()]
    counted = 0
    hired_total = 0
    for t in tokens:
        jm = re.match(r"^จ้าง(\d+)", t)
        if jm:
            n = int(jm.group(1))
            counted += n
            hired_total += n
        else:
            counted += 1
    return counted, hired_total, reported


class SheetLayout:
    """เก็บตำแหน่งคอลัมน์ + กลุ่มผู้รับเหมา ที่ตรวจจับได้จากไฟล์ (ไม่ hardcode เลขคอลัมน์
    เพราะแต่ละเดือนอาจสลับตำแหน่งกันได้ ตราบใดที่ยังใช้แถวชื่อกลุ่มแบบเดิม)"""

    def __init__(self, ws, header_row: int):
        self.ws = ws
        self.header_row = header_row
        self.headers = [normalize_header(ws.cell(row=header_row, column=c).value)
                         for c in range(1, ws.max_column + 1)]
        # เก็บตำแหน่งคอลัมน์แรกสุดที่เจอชื่อนั้น (กันปัญหาหัวคอลัมน์ซ้ำ เช่น
        # "Incharge"/"RN"/"PN"/"หมายเหตุ" ที่ปรากฏซ้ำหลายจุดในคนละกลุ่มผู้รับเหมา)
        self.col_index = {}
        for i, h in enumerate(self.headers):
            if h and h not in self.col_index:
                self.col_index[h] = i + 1

        group_row_idx = header_row - 1
        group_starts = []
        if group_row_idx >= 1:
            for c in range(2, ws.max_column + 1):
                v = normalize_header(ws.cell(row=group_row_idx, column=c).value)
                if v:
                    group_starts.append((c, v))
        self.groups = []
        for i, (col, label) in enumerate(group_starts):
            end = group_starts[i + 1][0] - 1 if i + 1 < len(group_starts) else ws.max_column
            self.groups.append({"label": label, "start": col, "end": end})

    def get(self, row: int, field_name: str):
        c = self.col_index.get(field_name)
        if not c:
            return None
        v = self.ws.cell(row=row, column=c).value
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip()

    def collect_range(self, row: int, start: int, end: int):
        out = []
        for c in range(start, end + 1):
            label = self.headers[c - 1] if c - 1 < len(self.headers) else ""
            if not label:
                continue
            v = self.ws.cell(row=row, column=c).value
            if v is not None and str(v).strip() != "":
                out.append((label, str(v).strip()))
        return out

    def analyze_duties(self, row: int):
        """สำหรับแถวนี้ คืน dict: {duty_name: {counted, hospital, contractor, unclear, breakdown}}"""
        result = {}
        for duty, cfg in CONFIG["DUTY_MAP"].items():
            raw = self.get(row, cfg["summary_header"])
            if not raw:
                continue
            counted, hired_total, reported = count_named_units(raw)
            named_inhouse = max(counted - hired_total, 0)

            matches = []
            for g in self.groups:
                for label, val in self.collect_range(row, g["start"], g["end"]):
                    if any(kw.lower() in label.lower() for kw in cfg["keywords"]):
                        matches.append((g["label"], label, val, classify_hire_source(label)))

            hospital_from_hire = sum(
                int(re.search(r"\d+", v).group()) for _, _, v, src in matches
                if src == "hospital" and re.search(r"\d+", v)
            )
            contractor_from_hire = sum(
                int(re.search(r"\d+", v).group()) for _, _, v, src in matches
                if src == "contractor" and re.search(r"\d+", v)
            )
            unclear_from_hire = max(hired_total - hospital_from_hire - contractor_from_hire, 0)

            result[duty] = {
                "raw": raw,
                "counted": counted,
                "hospital_units": named_inhouse + hospital_from_hire,
                "contractor_units": contractor_from_hire,
                "unclear_units": unclear_from_hire,
                "hire_detail": matches,
            }
        return result


def scan_workbooks(folder: str, target_date: datetime.date) -> list:
    events = []
    pattern = os.path.join(folder, "*.xlsx")
    files = [f for f in glob.glob(pattern) if not os.path.basename(f).startswith("~$")]

    if not files:
        log.warning("ไม่พบไฟล์ .xlsx ในโฟลเดอร์: %s", folder)
        return events

    for filepath in files:
        try:
            wb = openpyxl.load_workbook(filepath, data_only=True, read_only=False)
        except Exception as e:
            log.warning("เปิดไฟล์ไม่ได้ %s: %s", filepath, e)
            continue

        best = None
        best_score = -1
        for sheet_name in wb.sheetnames:
            ws_c = wb[sheet_name]
            hr = find_header_row(ws_c)
            if hr is None:
                continue
            headers_c = [normalize_header(ws_c.cell(row=hr, column=c).value)
                         for c in range(1, ws_c.max_column + 1)]
            score = sum(1 for f in CONFIG["EQUIPMENT_FIELDS"] if f in headers_c)
            if score > best_score:
                best_score = score
                best = (sheet_name, hr)

        if best is None:
            wb.close()
            continue

        sheet_name, header_row = best
        ws = wb[sheet_name]
        layout = SheetLayout(ws, header_row)
        date_col_name = next((h for h in layout.headers if "วันที่" in h), None)
        if date_col_name is None:
            wb.close()
            continue

        for r in range(header_row + 1, ws.max_row + 1):
            raw_date_val = ws.cell(row=r, column=layout.col_index[date_col_name]).value
            row_date = excel_date_to_gregorian(raw_date_val)
            if row_date != target_date:
                continue

            company = layout.get(r, "บริษัท")
            if not company:
                continue

            if "หยุด" in company:
                events.append({"holiday": company})
                continue

            main_info = {f: layout.get(r, f) for f in CONFIG["MAIN_FIELDS"]}
            equipment = {f: layout.get(r, f) for f in CONFIG["EQUIPMENT_FIELDS"]}
            equipment = {k: v for k, v in equipment.items() if v}
            duties = layout.analyze_duties(r)

            events.append({
                "company": company,
                "main": main_info,
                "equipment": equipment,
                "duties": duties,
            })

        wb.close()

    return events


def build_daily_borrow_checklist(real_events: list) -> dict:
    """รวมยอด 'ของที่ต้องเตรียม/ยืมจาก รพ.' ทั้งวัน แยกตามประเภทงาน (หู/ตา/ปอด)"""
    totals = {}
    for e in real_events:
        for duty, info in e.get("duties", {}).items():
            if duty not in totals:
                totals[duty] = {"hospital": 0, "contractor": 0, "unclear": 0}
            totals[duty]["hospital"] += info["hospital_units"]
            totals[duty]["contractor"] += info["contractor_units"]
            totals[duty]["unclear"] += info["unclear_units"]
    return totals


def format_message(target_date: datetime.date, events: list) -> str:
    thai_days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    thai_months = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                   "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    day_name = thai_days[target_date.weekday()]
    buddhist_year = target_date.year + 543
    date_str = f"{target_date.day} {thai_months[target_date.month]} {buddhist_year}"

    lines = [f"📋 แจ้งเตือนตารางออกหน่วย Mobile", f"วัน{day_name}ที่ {date_str}", ""]

    real_events = [e for e in events if "company" in e]
    holiday_events = [e for e in events if "holiday" in e]

    if not real_events and holiday_events:
        lines.append("🔴 วันนี้ไม่มีออกหน่วย (วันหยุด: %s)" % holiday_events[0]["holiday"])
        return "\n".join(lines)
    if not real_events:
        lines.append("ไม่พบข้อมูลออกหน่วยสำหรับวันนี้ในไฟล์ที่สแกน กรุณาตรวจสอบตารางด้วยตนเอง")
        return "\n".join(lines)

    # --- สรุปของที่ต้องยืม/เตรียมจาก รพ. ทั้งวัน (ตัวที่ผู้ใช้ต้องการเช็คก่อนใคร) ---
    totals = build_daily_borrow_checklist(real_events)
    lines.append("🏥 ของที่ต้องเตรียม/ยืมจาก รพ. วันนี้ (รวมทุกงาน):")
    any_borrow = False
    for duty, t in totals.items():
        if t["hospital"] > 0:
            any_borrow = True
            note = f" (+{t['unclear']} ไม่ชัดเจน โปรดตรวจสอบ)" if t["unclear"] else ""
            lines.append(f"   • {duty}: {t['hospital']} เครื่อง{note}")
    if not any_borrow:
        lines.append("   • ไม่มีรายการที่ต้องยืมจาก รพ. (งานวันนี้ใช้เครื่องผู้รับจ้างทั้งหมด)")
    lines.append("")
    lines.append(f"รายการงานวันนี้ ({len(real_events)} งาน):")
    lines.append("")

    for i, e in enumerate(real_events, 1):
        m = e["main"]
        lines.append(f"🏥 {i}. {e['company']}")
        if m.get("Location"):
            lines.append(f"   📍 สถานที่: {m['Location']}")
        if m.get("ประเภท Service"):
            lines.append(f"   🩺 บริการ: {m['ประเภท Service']}")
        if m.get("เวลา"):
            lines.append(f"   ⏰ เวลา: {m['เวลา']}")
        if m.get("จำนวนผู้เข้ารับการตรวจสุขภาพ/วัน"):
            lines.append(f"   👥 จำนวนผู้เข้ารับบริการ: {m['จำนวนผู้เข้ารับการตรวจสุขภาพ/วัน']}")

        if e["equipment"]:
            lines.append("   🧰 อุปกรณ์/บริการหลัก:")
            for k, v in e["equipment"].items():
                lines.append(f"      - {k}: {v}")

        if e["duties"]:
            lines.append("   🔍 รายละเอียดการนับเครื่อง/แหล่งที่มา:")
            for duty, info in e["duties"].items():
                parts = []
                if info["hospital_units"]:
                    parts.append(f"รพ.เตรียม {info['hospital_units']}")
                if info["contractor_units"]:
                    parts.append(f"ผู้รับจ้างเอามาเอง {info['contractor_units']}")
                if info["unclear_units"]:
                    parts.append(f"ไม่ชัดเจน {info['unclear_units']}")
                summary = " / ".join(parts) if parts else "ทั้งหมดใช้บุคลากร รพ. เอง"
                lines.append(f"      - {duty} (รวม {info['counted']}): {summary}")
                for grp, header, val, src in info["hire_detail"]:
                    tag = {"hospital": "🏥 เครื่อง รพ.", "contractor": "📦 ผู้รับจ้างเอามาเอง",
                           "unclear": "❓ ไม่ชัดเจน"}[src]
                    lines.append(f"          · [{grp}] {header} = {val} -> {tag}")
        lines.append("")

    return "\n".join(lines).strip()


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if requests is None:
        log.error("ไม่พบไลบรารี requests กรุณารัน: pip install requests --break-system-packages")
        return False
    # Telegram จำกัดความยาวข้อความ 4096 ตัวอักษรต่อครั้ง จึงตัดส่งเป็นหลายข้อความถ้ายาวเกิน
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=15)
            if resp.status_code != 200:
                log.error("ส่งข้อความไม่สำเร็จ: %s %s", resp.status_code, resp.text)
                ok = False
        except Exception as e:
            log.error("เกิดข้อผิดพลาดตอนส่ง Telegram: %s", e)
            ok = False
    if ok:
        log.info("ส่งข้อความสำเร็จ")
    return ok


def main():
    target_date = datetime.date.today() + datetime.timedelta(days=CONFIG["DAYS_AHEAD"])
    log.info("กำลังค้นหาตารางออกหน่วยของวันที่ %s", target_date.isoformat())

    events = scan_workbooks(CONFIG["SCHEDULE_FOLDER"], target_date)
    message = format_message(target_date, events)

    print(message)
    print("\n" + "=" * 50)

    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id = CONFIG["TELEGRAM_CHAT_ID"]
    if "ใส่_" in token or "ใส่_" in chat_id:
        log.warning("ยังไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID -> จะพิมพ์ผลลัพธ์ในหน้าจอเท่านั้น ไม่ส่งแจ้งเตือนจริง")
        return

    send_telegram(token, chat_id, message)


if __name__ == "__main__":
    main()