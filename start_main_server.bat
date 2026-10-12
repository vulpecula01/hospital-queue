@echo off
REM ==========================================================
REM start_main_server.bat
REM ==========================================================
REM ใช้ให้ main.py รันอัตโนมัติทุกครั้งที่เปิดเครื่อง (ไม่ต้องเปิด Command Prompt เอง)
REM
REM วิธีติดตั้งให้รันอัตโนมัติตอนเปิดเครื่อง (ทำครั้งเดียว):
REM   1. เปิด Task Scheduler (พิมพ์ในช่องค้นหา Windows)
REM   2. Create Task (ไม่ใช่ Basic Task) -> ตั้งชื่อ "Start Hospital Queue Server"
REM   3. แท็บ General:
REM        - ติ๊ก "Run whether user is logged on or not"
REM        - ติ๊ก "Run with highest privileges"
REM   4. แท็บ Triggers -> New -> Begin the task: "At startup"
REM   5. แท็บ Actions -> New -> Action: "Start a program"
REM        Program/script:  C:\hospital-queue\start_main_server.bat
REM        Start in:        C:\hospital-queue
REM   6. แท็บ Settings -> ติ๊ก "If the task fails, restart every: 1 minute"
REM        (กันกรณี MySQL ยังไม่พร้อมตอนเครื่องเพิ่งเปิด main.py จะได้ลองใหม่เอง)
REM   7. OK -> ใส่รหัสผ่าน Windows ของบัญชีที่ใช้ (ต้องมีสิทธิ์ admin)
REM
REM ทดสอบว่าใช้ได้: รีสตาร์ทเครื่อง แล้วเปิด http://10.121.111.233:8000/ ดูว่าเข้าได้เลยไหม
REM โดยไม่ต้อง login Windows หรือเปิด Command Prompt เองเลย
REM ==========================================================

cd /d C:\hospital-queue
uvicorn main:app --host 0.0.0.0 --port 8000
