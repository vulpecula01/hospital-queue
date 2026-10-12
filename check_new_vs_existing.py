"""
check_new_vs_existing.py
=========================
เช็คว่าไฟล์ผู้ป่วยใหม่ที่จะ import มี CN ที่ "ใหม่จริง" (ไม่เคยมีในฐานข้อมูล) กี่ตัว
กับ CN ที่ "ซ้ำกับของเดิม" (จะแค่ไปอัปเดตทับ ไม่ใช่แถวใหม่) กี่ตัว

วิธีใช้:
    pip install openpyxl pymysql --break-system-packages
    python check_new_vs_existing.py ไฟล์ใหม่.xlsx
"""

import sys
import re
import pymysql
import openpyxl

DB_CONFIG = dict(
    host="localhost",
    user="root",
    password="@Nurak@1234",
    database="hospital_queue_db",
    autocommit=True
)


def normalize_cn(cn):
    if not cn:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(cn)).upper()


def main():
    if len(sys.argv) < 2:
        print("วิธีใช้: python check_new_vs_existing.py <ไฟล์.xlsx>")
        sys.exit(1)

    filepath = sys.argv[1]
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active

    file_cns = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        cn_raw = row[0]
        cn_norm = normalize_cn(cn_raw)
        if cn_norm:
            file_cns.add(cn_norm)

    print(f"📄 CN ที่ไม่ซ้ำกันในไฟล์ (unique): {len(file_cns):,} ตัว")

    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT cn_normalized FROM patients")
            existing_cns = set(r[0] for r in cursor.fetchall())
    finally:
        connection.close()

    print(f"🗄️  CN ที่มีอยู่แล้วในฐานข้อมูลตอนนี้: {len(existing_cns):,} ตัว")

    genuinely_new = file_cns - existing_cns
    already_exist = file_cns & existing_cns

    print("=" * 60)
    print(f"✅ CN ที่ 'ใหม่จริง' ในไฟล์นี้ (ยังไม่เคยมีในระบบ): {len(genuinely_new):,} ตัว")
    print(f"🔁 CN ที่ 'มีอยู่แล้ว' ในระบบ (import แล้วจะแค่อัปเดตทับ): {len(already_exist):,} ตัว")
    print("=" * 60)

    if len(genuinely_new) == 0:
        print("\n⚠️  ไม่พบ CN ใหม่เลยแม้แต่ตัวเดียว!")
        print("   แปลว่าไฟล์นี้อาจเป็นไฟล์เดิมที่เคย import ไปแล้ว (Master ทั้งหมด)")
        print("   ไม่ใช่ไฟล์ 'เฉพาะรายชื่อใหม่' ตามที่คาดไว้")
    else:
        print(f"\n👉 หลัง import ไฟล์นี้ COUNT(*) ควรจะเพิ่มขึ้น {len(genuinely_new):,} แถว")

    if genuinely_new:
        print(f"\n🔍 ตัวอย่าง CN ที่ใหม่จริง (5 ตัวแรก): {list(genuinely_new)[:5]}")


if __name__ == "__main__":
    main()
