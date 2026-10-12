"""
create_user.py
================
สร้าง/แก้ไขผู้ใช้งานสำหรับระบบ Login — ต้องรันไฟล์นี้บนเครื่อง Server เดียวกับ main.py
(อ่านค่า .env เดียวกัน เพื่อ hash รหัสผ่านด้วย SECRET_KEY เดียวกับที่ main.py ใช้ตรวจสอบตอน login)

วิธีใช้:
    python create_user.py <username> <password> <full_name> <role>

ตัวอย่าง:
    python create_user.py admin "AdminPass456" "ผู้ดูแลระบบ" admin
    python create_user.py somchai "MyPass123" "สมชาย ใจดี" staff

role ต้องเป็น "admin" หรือ "staff" เท่านั้น
รันซ้ำกับ username เดิมได้ — จะอัปเดตรหัสผ่าน/ชื่อ/role ให้แทนการสร้างซ้ำ
"""

import sys
import os
import hashlib
import pymysql


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


DB_HOST = get_env("DB_HOST", "localhost")
DB_USER = get_env("DB_USER", "root")
DB_PASSWORD = get_env("DB_PASSWORD", "")
DB_NAME = get_env("DB_NAME", "hospital_queue_db")
SECRET_KEY = get_env("SECRET_KEY", "")

if not DB_PASSWORD or not SECRET_KEY:
    print("❌ ไม่พบไฟล์ .env หรือค่าที่จำเป็นยังไม่ครบ (ต้องมี DB_PASSWORD และ SECRET_KEY)")
    print("   ต้องรันสคริปต์นี้ในโฟลเดอร์เดียวกับ main.py ที่มีไฟล์ .env ตั้งค่าไว้แล้ว")
    sys.exit(1)


def hash_password(password: str) -> str:
    """ต้อง hash ด้วยวิธีเดียวกับใน main.py เป๊ะๆ ไม่งั้น login จะไม่ผ่าน"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', (password + SECRET_KEY).encode('utf-8'), salt, 100000)
    return salt.hex() + ':' + dk.hex()


def main():
    if len(sys.argv) < 5:
        print("วิธีใช้: python create_user.py <username> <password> <full_name> <role>")
        print("ตัวอย่าง: python create_user.py admin \"AdminPass456\" \"ผู้ดูแลระบบ\" admin")
        sys.exit(1)

    username, password, full_name, role = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

    if role not in ("admin", "staff"):
        print("❌ role ต้องเป็น 'admin' หรือ 'staff' เท่านั้น")
        sys.exit(1)

    if len(password) < 6:
        print("⚠️  คำเตือน: รหัสผ่านสั้นกว่า 6 ตัวอักษร แนะนำให้ตั้งยาวกว่านี้เพื่อความปลอดภัย")

    password_hash = hash_password(password)

    connection = pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME, autocommit=True
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO users (username, password_hash, full_name, role)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       password_hash = VALUES(password_hash),
                       full_name = VALUES(full_name),
                       role = VALUES(role),
                       is_active = 1""",
                (username, password_hash, full_name, role)
            )
        print(f"✅ สร้าง/อัปเดตผู้ใช้ '{username}' ({full_name}, role={role}) สำเร็จ")
        print(f"   ทดสอบ login ได้ที่หน้า login.html ด้วย username={username}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
