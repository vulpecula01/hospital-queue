"""
generate_tts_clips.py
======================
สร้างคลิปเสียงสั้นๆ ที่จำเป็นทั้งหมด (ตัวเลข, ตัวอักษร, ชื่อห้อง) ไว้ล่วงหน้า
เพื่อเอาไปใช้บนเครื่อง Server ที่ไม่มีอินเทอร์เน็ต

⚠️ ต้องรันสคริปต์นี้บนเครื่องที่มีอินเทอร์เน็ต (เช่น notebook ที่ใช้เน็ตมือถือ)
    ไม่ใช่บนเครื่อง Server หลัก (C:\\hospital-queue) เพราะเครื่องนั้นไม่มีเน็ต

วิธีใช้:
    pip install edge-tts
    python generate_tts_clips.py

เสร็จแล้วจะได้โฟลเดอร์ "tts_clips" ที่มีไฟล์ mp3 ทั้งหมด
ให้ copy โฟลเดอร์นี้ทั้งโฟลเดอร์ไปวางไว้ที่ C:\\hospital-queue\\tts_clips\\ (คู่กับ main.py) แทนที่ของเดิมทั้งหมด

📝 อัปเดตล่าสุด: เจอปัญหาที่เสียงฟังดูเหมือนผู้ชายตอนเล่นด้วยความเร็วปกติ (1.0x)
   เพราะก่อนหน้านี้ใช้การเร่งความเร็วเล่น (playbackRate) เพื่อพรางให้ฟังดูเป็นผู้หญิงมากขึ้น
   ซึ่งเป็นผลข้างเคียงที่ไม่ควรพึ่งพา — รอบนี้เลยเพิ่มการ "พิมพ์ยืนยันชื่อเสียงที่ใช้จริง" ทุกครั้งที่สร้าง
   ให้เช็คได้ชัดเจนว่าใช้เสียงถูกต้องจริง ไม่ต้องพึ่งการเร่งความเร็วมาช่วยพรางอีก
"""

import asyncio
import os
import edge_tts

OUTPUT_DIR = "tts_clips"

# 🌟 เสียงหลักที่ใช้ — ยืนยันแล้วว่าเป็นเสียงผู้หญิงตามรายชื่อทางการของ Microsoft Edge TTS
# ถ้าลองแล้วเสียง Premwadee ยังฟังดูไม่หญิงพอ ลองเปลี่ยนไปใช้ตัวเลือกสำรองด้านล่างแทนได้
VOICE_TH = "th-TH-AcharaNeural"       # เสียงผู้หญิงไทยที่ต้องการลองใช้
VOICE_TH_FALLBACK = "th-TH-PremwadeeNeural"  # 👈 เสียงสำรอง ใช้อัตโนมัติถ้า VOICE_TH ด้านบนใช้ไม่ได้ (กัน error ทั้งสคริปต์)

VOICE_EN = "en-US-JennyNeural"        # เสียงผู้หญิงอังกฤษ (ยืนยันแล้วว่าเป็นผู้หญิงชัดเจน)
# VOICE_EN_ALT = "en-US-AriaNeural"      # 👈 อีกตัวที่ Microsoft จัดว่าเป็นโทนอบอุ่น/เป็นกันเองกว่า Jenny ลองสลับดูได้
CLIP_RATE = "-15%"                    # 👈 ความเร็วพูด: ปรับตรงนี้เพื่อให้ช้า/เร็วขึ้น (ไม่กระทบระดับเสียง/เพศเสียง)
                                       # ตัวอย่าง: "-15%" = ช้าลง 15%, "+10%" = เร็วขึ้น 10%, "+0%" = ปกติ
                                       # ⚠️ ห้ามไปปรับความเร็วที่ playbackRate ในเว็บแทน เพราะจะทำให้เสียงเพี้ยนเป็นเสียงทุ้ม/ผู้ชาย

# ==========================================================
# รายการคลิปเสียงภาษาไทยที่ต้องสร้าง (key = ชื่อไฟล์, value = ข้อความ)
# ==========================================================
TH_CLIPS = {
    "prefix": "ขอเชิญหมายเลขคิว",

    "digit_0": "ศูนย์", "digit_1": "หนึ่ง", "digit_2": "สอง", "digit_3": "สาม", "digit_4": "สี่",
    "digit_5": "ห้า", "digit_6": "หก", "digit_7": "เจ็ด", "digit_8": "แปด", "digit_9": "เก้า",
    "letter_A": "เอ", "letter_W": "ดับเบิ้ลยู", "letter_M": "เอ็ม",

    "loc_VitalSign": "เชิญที่ห้องวัดสัญญาณชีพ ค่ะ",
    "loc_Blood": "เชิญที่ห้องเจาะเลือด ค่ะ",
    "loc_Eye": "เชิญที่ห้องตรวจตา ค่ะ",
    "loc_Ear": "เชิญที่ห้องตรวจหู ค่ะ",
    "loc_Lung": "เชิญที่ห้องเป่าปอด ค่ะ",
    "loc_EKG": "เชิญที่ห้องคลื่นไฟฟ้าหัวใจ ค่ะ",
    "loc_XRay": "เชิญที่แผนกเอกซเรย์ ค่ะ",
    "loc_Ultrasound": "เชิญที่ห้องอัลตราซาวด์ ค่ะ",
    "loc_Mammogram": "เชิญที่ห้องแมมโมแกรม ค่ะ",
    "loc_OBGYN": "เชิญที่ห้องตรวจสูตินรีเวช ค่ะ",
    "loc_Doctor": "เชิญที่ห้องตรวจ ค่ะ",
    "loc_Doctor_1": "เชิญที่ห้องตรวจหมายเลขหนึ่ง ค่ะ",
    "loc_Doctor_2": "เชิญที่ห้องตรวจหมายเลขสอง ค่ะ",
    "loc_Doctor_3": "เชิญที่ห้องตรวจหมายเลขสาม ค่ะ",
    "loc_Doctor_4": "เชิญที่ห้องตรวจหมายเลขสี่ ค่ะ",
    "loc_Doctor_5": "เชิญที่ห้องตรวจหมายเลขห้า ค่ะ",
    "loc_Doctor_6": "เชิญที่ห้องตรวจหมายเลขหก ค่ะ",
    "loc_Doctor_7": "เชิญที่ห้องตรวจหมายเลขเจ็ด ค่ะ",
    "loc_Doctor_8": "เชิญที่ห้องตรวจหมายเลขแปด ค่ะ",
    "loc_Cashier": "เชิญชำระเงิน ที่ช่องบริการการเงิน ค่ะ",
}

# ==========================================================
# รายการคลิปเสียงภาษาอังกฤษ (ใช้เมื่อชื่อคนไข้เป็นภาษาอังกฤษ)
# ==========================================================
EN_CLIPS = {
    "prefix": "Queue number",

    "digit_0": "zero", "digit_1": "one", "digit_2": "two", "digit_3": "three", "digit_4": "four",
    "digit_5": "five", "digit_6": "six", "digit_7": "seven", "digit_8": "eight", "digit_9": "nine",
    "letter_A": "A", "letter_W": "double-u", "letter_M": "M",

    "loc_VitalSign": "please proceed to the Vital Sign room.",
    "loc_Blood": "please proceed to the Blood Test room.",
    "loc_Eye": "please proceed to the Eye Exam room.",
    "loc_Ear": "please proceed to the Ear Exam room.",
    "loc_Lung": "please proceed to the Lung Function room.",
    "loc_EKG": "please proceed to the EKG room.",
    "loc_XRay": "please proceed to the X-Ray department.",
    "loc_Ultrasound": "please proceed to the Ultrasound room.",
    "loc_Mammogram": "please proceed to the Mammogram room.",
    "loc_OBGYN": "please proceed to the OB-GYN room.",
    "loc_Doctor": "please proceed to the doctor's room.",
    "loc_Doctor_1": "please proceed to doctor's room number one.",
    "loc_Doctor_2": "please proceed to doctor's room number two.",
    "loc_Doctor_3": "please proceed to doctor's room number three.",
    "loc_Doctor_4": "please proceed to doctor's room number four.",
    "loc_Doctor_5": "please proceed to doctor's room number five.",
    "loc_Doctor_6": "please proceed to doctor's room number six.",
    "loc_Doctor_7": "please proceed to doctor's room number seven.",
    "loc_Doctor_8": "please proceed to doctor's room number eight.",
    "loc_Cashier": "please proceed to the cashier counter.",
}


async def generate_clip(text, voice, filepath, fallback_voice=None):
    try:
        communicate = edge_tts.Communicate(text, voice, rate=CLIP_RATE)
        await communicate.save(filepath)
        print(f"  ✅ [{voice}] {filepath}")
    except Exception as e:
        if fallback_voice:
            print(f"  ⚠️  [{voice}] ใช้ไม่ได้ ({type(e).__name__}) — ลองเสียงสำรอง [{fallback_voice}] แทน")
            communicate = edge_tts.Communicate(text, fallback_voice, rate=CLIP_RATE)
            await communicate.save(filepath)
            print(f"  ✅ [{fallback_voice}] {filepath} (ใช้เสียงสำรอง)")
        else:
            raise


async def main():
    print(f"🎙️  เสียงไทยที่ใช้: {VOICE_TH} (สำรอง: {VOICE_TH_FALLBACK})")
    print(f"🎙️  เสียงอังกฤษที่ใช้: {VOICE_EN}")
    print("=" * 60)

    th_dir = os.path.join(OUTPUT_DIR, "th")
    en_dir = os.path.join(OUTPUT_DIR, "en")
    os.makedirs(th_dir, exist_ok=True)
    os.makedirs(en_dir, exist_ok=True)

    print("🔊 กำลังสร้างคลิปเสียงภาษาไทย...")
    for name, text in TH_CLIPS.items():
        filepath = os.path.join(th_dir, f"{name}.mp3")
        await generate_clip(text, VOICE_TH, filepath, fallback_voice=VOICE_TH_FALLBACK)

    print("\n🔊 กำลังสร้างคลิปเสียงภาษาอังกฤษ...")
    for name, text in EN_CLIPS.items():
        filepath = os.path.join(en_dir, f"{name}.mp3")
        await generate_clip(text, VOICE_EN, filepath)

    print("\n" + "=" * 60)
    print(f"✅ เสร็จสิ้น! ไฟล์ทั้งหมดอยู่ในโฟลเดอร์ '{OUTPUT_DIR}/'")
    print(f"   สร้างด้วยเสียง: ไทย={VOICE_TH} (หรือสำรอง {VOICE_TH_FALLBACK} ถ้าตัวหลักใช้ไม่ได้) / อังกฤษ={VOICE_EN}")
    print(f"\n👉 ลองฟังไฟล์ตัวอย่างก่อน copy ไปใช้จริง:")
    print(f"   เปิดไฟล์ {th_dir}/prefix.mp3 ด้วย Media Player เช็คว่าเป็นเสียงผู้หญิงจริงไหม")
    print(f"\n👉 ขั้นตอนต่อไป: copy โฟลเดอร์ '{OUTPUT_DIR}' ทั้งโฟลเดอร์ไปวางไว้ที่")
    print(f"   C:\\hospital-queue\\{OUTPUT_DIR}\\  (คู่กับ main.py) แทนที่ของเดิมทั้งหมด ผ่าน USB หรือแชร์ไฟล์ในวง LAN")


if __name__ == "__main__":
    asyncio.run(main())