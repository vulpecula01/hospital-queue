@echo off
REM ==========================================================
REM open_register_silent_print.bat
REM ==========================================================
REM เปิดหน้า register.html ด้วย Chrome โหมดพิเศษ "kiosk-printing"
REM ทำให้ทุกครั้งที่หน้าเว็บสั่ง window.print() จะ "พิมพ์ทันที" ไปที่เครื่องพิมพ์ default
REM โดยไม่มีหน้าต่างเลือกเครื่องพิมพ์/ตั้งค่าใดๆ โผล่ขึ้นมาเลย
REM
REM วิธีใช้:
REM   1. ตั้งเครื่องพิมพ์ใบเสร็จเป็น Default printer ก่อน (Windows Settings -> Printers)
REM   2. ดับเบิลคลิกไฟล์นี้ทุกครั้งที่จะเปิดหน้าลงทะเบียน (แทนการเปิด Chrome ปกติ)
REM   3. (แนะนำ) คลิกขวาไฟล์นี้ -> Send to -> Desktop (create shortcut)
REM      แล้วเปลี่ยนไอคอนให้สวยงาม จะได้ใช้งานง่ายเหมือนแอปทั่วไป
REM
REM ⚠️ ต้องปิด Chrome ทุกหน้าต่างที่เปิดอยู่ก่อน ถึงจะเปิดด้วย flag นี้ได้ผล
REM    (ถ้า Chrome เปิดอยู่แล้ว หน้าต่างใหม่จะใช้ process เดิมที่ไม่มี flag นี้)
REM ==========================================================

taskkill /F /IM chrome.exe /T >nul 2>&1
timeout /t 1 /nobreak >nul

start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --kiosk-printing --new-window "http://10.121.111.233:8000/register.html"
