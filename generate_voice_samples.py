"""
generate_voice_samples.py
===========================
สร้าง "ตัวอย่างเสียง" หลายแบบ (เสียงผู้หญิงไทย) มาเทียบกัน
เพื่อเลือกว่าแบบไหนฟังดูน่ารัก/เป็นผู้หญิงชัดเจนที่สุด
ก่อนจะเอาไปใช้จริงกับ generate_tts_clips.py

⚠️ ต้องรันบนเครื่องที่มีอินเทอร์เน็ต (เหมือนตอนสร้างคลิปจริง)
    ไม่ใช่บนเครื่อง Server หลัก

วิธีใช้:
    pip install edge-tts
    python generate_voice_samples.py

เสร็จแล้วจะได้โฟลเดอร์ "voice_samples/" มีไฟล์ mp3 ให้ลองฟังทีละไฟล์
ฟังจบแล้วบอกชื่อไฟล์ที่ชอบที่สุด (เช่น "02_premwadee_pitch_up.mp3")
แล้วเอาไปบอก Claude เพื่อไปแก้ generate_tts_clips.py ให้ใช้ค่านั้นเป็นตัวจริง
"""

import asyncio
import os
import edge_tts

OUTPUT_DIR = "voice_samples"

# ประโยคทดสอบ — ยาวขึ้นหน่อยเพื่อให้ได้ยินโทนเสียงจริงชัดๆ
TEST_TEXT = "ขอเชิญหมายเลขคิว เอ ศูนย์ ศูนย์ หนึ่ง เชิญที่ห้องเจาะเลือด ค่ะ"

# 🔎 อัปเดต: ยืนยันแล้วว่า th-TH-AcharaNeural ใช้งานไม่ได้แล้วในตอนนี้ (NoAudioReceived ตลอด)
# เหลือ th-TH-PremwadeeNeural เป็นเสียงผู้หญิงไทยตัวเดียวที่ยังใช้งานได้จริง
# เลยลองไล่ปรับ pitch (ความสูงเสียง) ของ Premwadee หลายระดับแทน เพื่อหาโทนที่ฟังดูน่ารักที่สุด
SAMPLES = [
    {"name": "01_premwadee_normal",     "voice": "th-TH-PremwadeeNeural", "pitch": "+0Hz"},
    {"name": "02_premwadee_pitch_up10", "voice": "th-TH-PremwadeeNeural", "pitch": "+10Hz"},
    {"name": "03_premwadee_pitch_up20", "voice": "th-TH-PremwadeeNeural", "pitch": "+20Hz"},
]

RATE = "-15%"  # ใช้ค่าเดียวกับสคริปต์เดิมที่ใช้งานได้จริง


async def generate_sample(sample, out_dir):
    filepath = os.path.join(out_dir, f"{sample['name']}.mp3")
    try:
        communicate = edge_tts.Communicate(
            TEST_TEXT,
            sample["voice"],
            rate=RATE,
            pitch=sample["pitch"],
        )
        await communicate.save(filepath)
        print(f"  ✅ {sample['name']}.mp3   (voice={sample['voice']}, pitch={sample['pitch']})")
    except Exception as e:
        print(f"  ❌ {sample['name']}.mp3 พลาด: {type(e).__name__} — ข้ามไปตัวถัดไป")


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("🔊 กำลังสร้างตัวอย่างเสียงให้ลองเทียบ...")
    print("=" * 60)
    for sample in SAMPLES:
        await generate_sample(sample, OUTPUT_DIR)

    print("=" * 60)
    print(f"✅ เสร็จแล้ว! ลองเปิดไฟล์ในโฟลเดอร์ '{OUTPUT_DIR}/' ฟังทีละไฟล์")
    print("   แล้วบอกชื่อไฟล์ที่ชอบที่สุด จะได้เอาไปตั้งเป็นค่าจริงใน generate_tts_clips.py")


if __name__ == "__main__":
    asyncio.run(main())