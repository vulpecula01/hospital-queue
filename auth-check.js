/**
 * auth-check.js
 * ==============
 * รวมไว้ใน <head> หรือต้น <body> ของทุกหน้าที่ต้อง login ก่อนใช้งาน
 * ตัวอย่าง: <script src="auth-check.js"></script>
 *
 * ทำ 3 อย่าง:
 * 1. เช็คว่า login อยู่ไหม (เรียก /api/auth/check) ถ้าไม่ได้ login → เด้งไปหน้า login.html ทันที
 * 2. แสดงชื่อผู้ใช้ + ปุ่ม "ออกจากระบบ" ไว้มุมขวาบนของทุกหน้า
 * 3. เก็บ current_user ไว้ใน window.currentUser ให้หน้านั้นๆ เอาไปใช้ต่อได้ (เช่น log ว่าใครกดปุ่มอะไร)
 */

(function () {
    // 🌐 ใช้ที่อยู่ของหน้านี้เอง (origin) แทนการฝัง IP ตายตัว
    // กันปัญหา cookie ถูกบล็อกตอนเปิดหน้าด้วย URL ที่ไม่ตรงกับ IP ที่ฝังไว้ (เช่น localhost vs 10.121.111.233)
    const API_BASE = window.location.origin;

    async function checkAuth() {
        try {
            const res = await fetch(`${API_BASE}/api/auth/check`, {
                credentials: "include"
            });

            if (!res.ok) {
                redirectToLogin();
                return;
            }

            const data = await res.json();
            window.currentUser = data;
            renderUserBadge(data);
        } catch (err) {
            console.error("ตรวจสอบสถานะ login ไม่สำเร็จ:", err);
            redirectToLogin();
        }
    }

    function redirectToLogin() {
        const currentPage = window.location.pathname.split('/').pop();
        window.location.href = `login.html?redirect=${encodeURIComponent(currentPage)}`;
    }

    function renderUserBadge(user) {
        if (!document.body) {
            document.addEventListener("DOMContentLoaded", () => renderUserBadge(user));
            return;
        }
        const badge = document.createElement("div");
        badge.id = "auth-user-badge";
        badge.style.cssText = "position:fixed; top:8px; right:8px; z-index:99999; display:flex; align-items:center; gap:8px; background:rgba(15,23,42,0.9); border:1px solid #475569; border-radius:12px; padding:6px 12px; font-family:sans-serif; font-size:12px; color:#e2e8f0; box-shadow:0 4px 12px rgba(0,0,0,0.3);";
        badge.innerHTML = `
            <span>👤 ${user.full_name || user.username}</span>
            <button id="auth-logout-btn" style="background:#dc2626; color:white; border:none; border-radius:8px; padding:4px 10px; font-weight:bold; cursor:pointer; font-size:11px;">
                ออกจากระบบ
            </button>
        `;
        document.body.appendChild(badge);

        document.getElementById("auth-logout-btn").addEventListener("click", async () => {
            try {
                await fetch(`${API_BASE}/api/logout`, {
                    method: "POST",
                    credentials: "include"
                });
            } catch (e) { /* ไม่ต้องสนใจ error ตอน logout */ }
            localStorage.removeItem("display_name");
            window.location.href = "login.html";
        });
    }

    // เช็คทันทีที่โหลดสคริปต์นี้ (ไม่ต้องรอ DOMContentLoaded เพื่อเด้งไป login ให้เร็วที่สุดถ้ายังไม่ได้ login)
    checkAuth();
})();