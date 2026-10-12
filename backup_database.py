"""
backup_database.py
====================
สำรองข้อมูลฐานข้อมูล hospital_queue_db อัตโนมัติ — อ่านค่าเชื่อมต่อจาก .env เดียวกับ main.py
(ไม่ต้องเขียนรหัสผ่านซ้ำไว้อีกไฟล์ ปลอดภัยกว่า)

ต้องรันในโฟลเดอร์เดียวกับ main.py (ที่มีไฟล์ .env อยู่)

เก็บไฟล์สำรองไว้ 30 วันล่าสุด (ไฟล์เก่ากว่านั้นลบทิ้งอัตโนมัติ กันฮาร์ดดิสก์เต็ม)

วิธีตั้งให้รันอัตโนมัติทุกวัน (ทำครั้งเดียว):
    1. เปิด Task Scheduler (พิมพ์ในช่องค้นหา Windows)
    2. Create Basic Task -> ตั้งชื่อ "Backup Hospital Queue DB"
    3. Trigger: Daily -> เลือกเวลาที่คนไข้น้อย เช่น ตี 2
    4. Action: Start a program
       Program/script:  python
       Add arguments:   C:\\hospital-queue\\backup_database.py
       Start in:        C:\\hospital-queue
    5. Finish
"""

import os
import sys
import subprocess
import datetime
import glob

KEEP_DAYS = 30
BACKUP_DIR = "backups"

# 👇 แก้ path นี้ให้ตรงกับเครื่องจริง ถ้า mysqldump.exe อยู่คนละที่
MYSQLDUMP_PATH = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe"


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
    return _env.get(key) or os.environ.get(key) or default


def main():
    db_host = get_env("DB_HOST", "localhost")
    db_user = get_env("DB_USER", "root")
    db_password = get_env("DB_PASSWORD", "")
    db_name = get_env("DB_NAME", "hospital_queue_db")

    if not db_password:
        print("❌ ไม่พบ DB_PASSWORD ใน .env — ต้องรันสคริปต์นี้ในโฟลเดอร์เดียวกับ main.py ที่มี .env ตั้งค่าไว้แล้ว")
        sys.exit(1)

    if not os.path.exists(MYSQLDUMP_PATH):
        print(f"❌ ไม่พบ mysqldump.exe ที่ '{MYSQLDUMP_PATH}'")
        print("   แก้ตัวแปร MYSQLDUMP_PATH ในสคริปต์นี้ให้ตรงกับที่ติดตั้ง MySQL จริงบนเครื่อง")
        sys.exit(1)

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, f"backup_{timestamp}.sql")

    print(f"📦 กำลังสำรองข้อมูล -> {backup_file}")

    # ใช้ environment variable MYSQL_PWD แทนการใส่รหัสผ่านใน command line ตรงๆ
    # (ป้องกันไม่ให้รหัสผ่านโผล่ใน process list ของเครื่อง ปลอดภัยกว่า)
    env = os.environ.copy()
    env["MYSQL_PWD"] = db_password

    try:
        with open(backup_file, "w", encoding="utf-8") as f:
            result = subprocess.run(
                [MYSQLDUMP_PATH, "-h", db_host, "-u", db_user, db_name],
                stdout=f,
                stderr=subprocess.PIPE,
                env=env,
                text=True
            )

        if result.returncode == 0:
            size_kb = os.path.getsize(backup_file) / 1024
            print(f"✅ สำรองข้อมูลสำเร็จ ({size_kb:.1f} KB)")
        else:
            print(f"❌ เกิดข้อผิดพลาดระหว่างสำรองข้อมูล:\n{result.stderr}")
            sys.exit(1)

    except Exception as e:
        print(f"❌ รัน mysqldump ไม่สำเร็จ: {type(e).__name__}: {e}")
        sys.exit(1)

    # ลบไฟล์ backup ที่เก่ากว่า KEEP_DAYS วัน กันฮาร์ดดิสก์เต็ม
    cutoff = datetime.datetime.now() - datetime.timedelta(days=KEEP_DAYS)
    deleted_count = 0
    for old_file in glob.glob(os.path.join(BACKUP_DIR, "backup_*.sql")):
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(old_file))
        if mtime < cutoff:
            os.remove(old_file)
            deleted_count += 1

    if deleted_count:
        print(f"🗑️  ลบไฟล์ backup เก่าที่เกิน {KEEP_DAYS} วันไปแล้ว {deleted_count} ไฟล์")

    print("เสร็จสิ้น")


if __name__ == "__main__":
    main()
