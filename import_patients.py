"""
import_patients_fast.py
========================
เวอร์ชันเร็ว — ใช้ batch insert (executemany) แทนการ insert ทีละแถว
เหมาะกับตอนตารางว่างเปล่า/เพิ่ง TRUNCATE (ยังไม่มีข้อมูลเดิมให้ต้องเช็คซ้ำทีละแถว)

ก่อนรันสคริปต์นี้ แนะนำให้ล้างตารางให้สะอาดก่อน:
    TRUNCATE TABLE patients;

วิธีใช้:
    pip install openpyxl pymysql --break-system-packages
    python import_patients_fast.py book1.xlsx
"""

import sys
import re
import time
import datetime
import pymysql
import openpyxl

BIRTH_DATE_COLUMN = "birth_date"  # 👈 เปลี่ยนตรงนี้ถ้าตารางใช้ชื่อคอลัมน์อื่น

DB_CONFIG = dict(
    host="localhost",
    user="root",
    password="@Nurak@1234",
    database="hospital_queue_db",
    autocommit=False
)

BATCH_SIZE = 5000  # ยิ่งมากยิ่งเร็ว (แต่กิน memory มากขึ้นด้วย) 5000-10000 กำลังดี


def normalize_cn(cn: str) -> str:
    if not cn:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(cn)).upper()


def parse_dob(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        d = value.date()
    elif isinstance(value, datetime.date):
        d = value
    else:
        text = str(value).strip()
        d = None
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
            try:
                d = datetime.datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            return None
    if d.year > 2400:
        d = d.replace(year=d.year - 543)
    return d


def main():
    if len(sys.argv) < 2:
        print("วิธีใช้: python import_patients_fast.py <ไฟล์.xlsx>")
        sys.exit(1)

    filepath = sys.argv[1]
    start_time = time.time()

    print(f"📂 กำลังเปิดไฟล์: {filepath} ...")
    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
    ws = wb.active

    connection = pymysql.connect(**DB_CONFIG)

    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM patients")
            actual_columns = {row[0] for row in cursor.fetchall()}
    except Exception as e:
        print(f"❌ เชื่อมต่อฐานข้อมูลไม่สำเร็จ: {e}")
        connection.close()
        sys.exit(1)

    required = {"cn", "cn_normalized", "hn", "prefix", "first_name", "last_name", BIRTH_DATE_COLUMN, "gender"}
    missing = required - actual_columns
    if missing:
        print(f"❌ ตาราง patients ไม่มีคอลัมน์: {missing}")
        print(f"   คอลัมน์จริงที่มี: {sorted(actual_columns)}")
        connection.close()
        sys.exit(1)

    insert_sql = f"""
        INSERT INTO patients (cn, cn_normalized, hn, prefix, first_name, last_name, {BIRTH_DATE_COLUMN}, gender)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            cn = VALUES(cn),
            hn = VALUES(hn),
            prefix = VALUES(prefix),
            first_name = VALUES(first_name),
            last_name = VALUES(last_name),
            {BIRTH_DATE_COLUMN} = VALUES({BIRTH_DATE_COLUMN}),
            gender = VALUES(gender)
    """

    print("✅ โครงสร้างตารางตรงกัน เริ่ม import แบบ batch...")

    batch = []
    total_processed = 0
    total_skipped = 0
    total_error_rows = 0

    try:
        with connection.cursor() as cursor:
            for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                cn_raw, hn, dob_raw, gender, prefix, first_name, last_name = (list(row) + [None] * 7)[:7]

                if not cn_raw:
                    continue

                cn_norm = normalize_cn(cn_raw)
                if not cn_norm:
                    total_skipped += 1
                    continue

                dob = parse_dob(dob_raw)
                batch.append((str(cn_raw).strip(), cn_norm, hn, prefix, first_name, last_name, dob, gender))
                total_processed += 1

                if len(batch) >= BATCH_SIZE:
                    try:
                        cursor.executemany(insert_sql, batch)
                        connection.commit()
                    except Exception as e:
                        # ถ้าทั้ง batch พัง ให้ rollback แล้วลองทีละแถวแทน (ช้าลงเฉพาะ batch ที่มีปัญหา)
                        connection.rollback()
                        print(f"⚠️  Batch ที่แถว ~{row_num} มีปัญหา ({e}) — ลองทีละแถวแทน...")
                        for single_row in batch:
                            try:
                                cursor.execute(insert_sql, single_row)
                            except Exception as e2:
                                total_error_rows += 1
                        connection.commit()

                    elapsed = time.time() - start_time
                    speed = total_processed / elapsed if elapsed > 0 else 0
                    print(f"   ...import แล้ว {total_processed:,} แถว ({speed:,.0f} แถว/วินาที)")
                    batch = []

            # flush batch สุดท้ายที่เหลือ
            if batch:
                try:
                    cursor.executemany(insert_sql, batch)
                    connection.commit()
                except Exception as e:
                    connection.rollback()
                    print(f"⚠️  Batch สุดท้ายมีปัญหา ({e}) — ลองทีละแถวแทน...")
                    for single_row in batch:
                        try:
                            cursor.execute(insert_sql, single_row)
                        except Exception:
                            total_error_rows += 1
                    connection.commit()
    finally:
        connection.close()

    elapsed_total = time.time() - start_time

    with pymysql.connect(**DB_CONFIG) as check_conn:
        with check_conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM patients")
            final_count = cursor.fetchone()[0]

    print("=" * 60)
    print(f"✅ Import เสร็จสิ้นใน {elapsed_total:.1f} วินาที")
    print(f"   แถวที่ประมวลผล : {total_processed:,}")
    print(f"   ข้าม (ไม่มี CN) : {total_skipped:,}")
    print(f"   Error          : {total_error_rows:,}")
    print(f"   จำนวนแถวในตาราง patients ตอนนี้ : {final_count:,}")
    print("=" * 60)


if __name__ == "__main__":
    main()