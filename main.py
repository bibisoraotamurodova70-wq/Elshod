import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
import urllib.request
import ssl
from contextlib import contextmanager
from datetime import datetime, timezone

# SSL xatoliklarini oldini olish uchun (Pydroid 3 uchun maxsus)
ssl._create_default_https_context = ssl._create_unverified_context

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logsumexp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Diplom chizish uchun kutubxonani chaqiramiz
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


# ============================================================
# SOZLAMALAR
# ============================================================

# Token faqat server muhit o'zgaruvchisidan olinadi (kod ichida ko'rsatilmaydi)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("DIQQAT: BOT_TOKEN muhit o'zgaruvchisida topilmadi!")

ADMIN_IDS = {8879728979}

if os.getenv("ADMIN_IDS"):
    ADMIN_IDS = {
        int(value.strip())
        for value in os.environ["ADMIN_IDS"].split(",")
        if value.strip()
    }

DB_PATH = os.getenv("DB_PATH", "mock_bot.sqlite3")
MAX_QUESTIONS = 150
MAX_JSON_BYTES = 256_000

THETA_PRIOR_SD = 2.0
DIFFICULTY_PRIOR_SD = 2.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("mock_bot")

EDITING_NAMES = set()


# ============================================================
# MA'LUMOTLAR BAZASI
# ============================================================

@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def init_db():
    with database() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                real_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                questions_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                pdf_file_id TEXT,
                created_at INTEGER NOT NULL,
                opened_at INTEGER,
                calibration_json TEXT
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL REFERENCES tests(id),
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT,
                started_at INTEGER NOT NULL,
                deadline INTEGER NOT NULL,
                answers_json TEXT NOT NULL,
                submitted_at INTEGER,
                result_json TEXT,
                UNIQUE(test_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_test ON attempts(test_id);
            """
        )

def now(): return int(time.time())

def get_test(test_id):
    with database() as db:
        return db.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()

def get_attempt(test_id, user_id):
    with database() as db:
        return db.execute("SELECT * FROM attempts WHERE test_id = ? AND user_id = ?", (test_id, user_id)).fetchone()

def questions_of(test):
    return json.loads(test["questions_json"])


# ============================================================
# DIPLOM YASASH FUNKSIYASI (NOLDAN CHIZADI)
# ============================================================

def generate_diploma(name, percent, place, date_str):
    if not HAS_PILLOW: 
        return "NO_PILLOW"
    
    try:
        width, height = 1000, 700
        img = Image.new('RGB', (width, height), color=(250, 250, 252))
        draw = ImageDraw.Draw(img)
        
        colors = {
            1: (218, 165, 32),  # 1-o'rin (Oltin)
            2: (169, 169, 169), # 2-o'rin (Kumush)
            3: (205, 127, 50)   # 3-o'rin (Bronza)
        }
        border_color = colors.get(place, (0, 0, 0))
        
        # Ramka chizish
        draw.rectangle([30, 30, width-30, height-30], outline=border_color, width=20)
        draw.rectangle([60, 60, width-60, height-60], outline=(0, 0, 0), width=2)
        
        font_file = "Roboto-Bold.ttf"
        if not os.path.exists(font_file):
            try:
                urllib.request.urlretrieve("https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Bold.ttf", font_file)
            except:
                pass
        
        try:
            font_title = ImageFont.truetype(font_file, 70)
            font_name = ImageFont.truetype(font_file, 80)
            font_text = ImageFont.truetype(font_file, 40)
        except:
            font_title = ImageFont.load_default()
            font_name = ImageFont.load_default()
            font_text = ImageFont.load_default()
            
        draw.text((width//2, 120), "D I P L O M", fill=border_color, font=font_title, anchor="mm")
        draw.text((width//2, 230), f"Online testda {percent:.1f}% natija bilan", fill=(50,50,50), font=font_text, anchor="mm")
        draw.text((width//2, 290), f"{place}-o'rinni qo'lga kiritganligi uchun", fill=(50,50,50), font=font_text, anchor="mm")
        
        draw.text((width//2, 410), str(name), fill=(0,0,0), font=font_name, anchor="mm")
        draw.line([250, 460, 750, 460], fill=border_color, width=4)
        
        draw.text((width//2, 530), "Ushbu diplom bilan taqdirlanadi", fill=(50,50,50), font=font_text, anchor="mm")
        
        draw.text((200, 610), f"Sana: {date_str}", fill=(100,100,100), font=font_text, anchor="mm")
        draw.text((800, 610), "Mock Bot", fill=(100,100,100), font=font_text, anchor="mm")
        
        bio = io.BytesIO()
        bio.name = 'diploma.jpg'
        img.save(bio, format='JPEG')
        bio.seek(0)
        return bio
    except Exception as e:
        return str(e)


# ============================================================
# TEKSHIRUVLAR VA YORDAMCHI FUNKSIYALAR
# ============================================================

async def private_only(update):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Botdan shaxsiy chatda foydalaning.")
        return False
    return True

async def admin_only(update):
    if not await private_only(update): return False
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Bu buyruq faqat o'qituvchi uchun.")
        return False
    return True

def command_id(context):
    if not context.args: raise ValueError("Test ID raqamini kiriting. Masalan: /open 1")
    try: value = int(context.args[0])
    except: raise ValueError("Test ID butun son bo'lishi kerak.")
    return value

async def send_csv(update, filename, header, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    bio = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    bio.name = filename
    await update.message.reply_document(document=bio)

def get_grade(score: float) -> str:
    if score >= 70.0: return "A+"
    elif score >= 65.0: return "A"
    elif score >= 60.0: return "B+"
    elif score >= 55.0: return "B"
    elif score >= 50.0: return "C+"
    elif score >= 46.0: return "C"
    else: return "Daraja ololmadingiz"


# ============================================================
# HISOBLASH (RASCH MODELI)
# ============================================================

def estimate_eap(correct_count, difficulties):
    grid = np.linspace(-12.0, 12.0, 2401)
    logits = grid[:, None] - difficulties[None, :]
    log_posterior = (correct_count * grid - np.logaddexp(0.0, logits).sum(axis=1) - 0.5 * (grid / THETA_PRIOR_SD) ** 2)
    weights = np.exp(log_posterior - logsumexp(log_posterior))
    theta = float(np.sum(weights * grid))
    variance = float(np.sum(weights * (grid - theta) ** 2))
    return {
        "theta": theta,
        "posterior_sd": math.sqrt(max(variance, 0.0)),
        "mock_score": 50.0 + 10.0 * theta,
    }

def calibrate_difficulties(matrix):
    people, items = matrix.shape
    person_totals = matrix.sum(axis=1)
    item_totals = matrix.sum(axis=0)

    def objective(parameters):
        theta = parameters[:people]
        u = parameters[people:]
        b = u - u.mean()
        logits = theta[:, None] - b[None, :]
        probability = expit(logits)
        loss = (np.logaddexp(0.0, logits).sum() - np.dot(theta, person_totals) + np.dot(b, item_totals)
                + 0.5 * np.sum(theta ** 2) / THETA_PRIOR_SD ** 2 + 0.5 * np.sum(b ** 2) / DIFFICULTY_PRIOR_SD ** 2
                + 0.5 * items * u.mean() ** 2)
        gradient_theta = (probability.sum(axis=1) - person_totals + theta / THETA_PRIOR_SD ** 2)
        gradient_b = (item_totals - probability.sum(axis=0) + b / DIFFICULTY_PRIOR_SD ** 2)
        gradient_u = gradient_b - gradient_b.mean() + u.mean()
        return float(loss), np.concatenate([gradient_theta, gradient_u])

    solution = minimize(objective, np.zeros(people + items), method="L-BFGS-B", jac=True, options={"maxiter": 2000, "ftol": 1e-10})
    if not solution.success or not np.isfinite(solution.x).all():
        raise RuntimeError("Kalibrlash amalga oshmadi.")
    u = solution.x[people:]
    return u - u.mean()

def calculate_results(questions, attempts):
    keys = [question["key"] for question in questions]
    matrix = np.array([[int(answer == key) for answer, key in zip(json.loads(attempt["answers_json"]), keys)] for attempt in attempts], dtype=float)

    if len(attempts) < 2 or np.all(matrix == 0) or np.all(matrix == 1):
        difficulties = np.zeros(len(questions))
    else:
        try: difficulties = calibrate_difficulties(matrix)
        except: difficulties = np.zeros(len(questions))

    item_proportions = matrix.mean(axis=0) if len(attempts) > 0 else np.zeros(len(questions))
    results = {}
    score_cache = {}
    total_q = len(questions)

    for attempt, row in zip(attempts, matrix):
        correct = int(row.sum())
        if correct not in score_cache:
            raw_result = estimate_eap(correct, difficulties)
            if correct == 0: raw_result["mock_score"] = 0.0
            elif correct == total_q: raw_result["mock_score"] = 100.0
            else: raw_result["mock_score"] = max(0.0, min(100.0, raw_result["mock_score"]))
            score_cache[correct] = raw_result

        results[attempt["id"]] = {**score_cache[correct], "correct": correct, "total": total_q, "percent": 100.0 * correct / total_q}

    calibration = {"participants": len(attempts), "b": difficulties.tolist(), "item_correct_proportions": item_proportions.tolist(), "calculated_at": now()}
    return results, calibration


# ============================================================
# ASOSIY MENYU GENERATORI (DRY)
# ============================================================

def get_main_menu(uid):
    with database() as db:
        user_row = db.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
        
    if not user_row:
        text = (
            "👋 Assalomu alaykum!\n\n"
            "Ona tili Mock Test botiga xush kelibsiz.\n\n"
            "⚠️ <b>Testlarda qatnashish va reyting jadvalida chiqish uchun, iltimos, to'liq ism-familiyangizni yozib yuboring.</b>\n\n"
            "<i>Masalan: Elshodbek Xolmatov</i>"
        )
        return text, None
    
    keyboard = []
    with database() as db:
        open_tests = db.execute("SELECT id, title, duration_minutes FROM tests WHERE status = 'open'").fetchall()
        for t in open_tests:
            attempt = db.execute("SELECT submitted_at, deadline FROM attempts WHERE test_id = ? AND user_id = ?", (t['id'], uid)).fetchone()
            if attempt and (attempt['submitted_at'] or now() > attempt['deadline']):
                continue 
            keyboard.append([InlineKeyboardButton(f"📝 {t['title']} ({t['duration_minutes']} daq)", callback_data=f"join_{t['id']}")])
            
        has_results = db.execute(
            """SELECT 1 FROM attempts a JOIN tests t ON a.test_id = t.id 
               WHERE a.user_id = ? AND t.status = 'scored' AND a.result_json IS NOT NULL LIMIT 1""", (uid,)
        ).fetchone()
        
        if has_results:
            keyboard.append([InlineKeyboardButton("📊 Mening natijalarim", callback_data="myresults")])
    
    keyboard.append([InlineKeyboardButton("✏️ Ism-familiyani tahrirlash", callback_data="editname")])
            
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    text = f"👤 <b>Foydalanuvchi:</b> {user_row['real_name']}\n\n👇 <b>Quyidagi tugmalardan keraklisini tanlang:</b>"
    
    if len(keyboard) == 1: 
        text = f"👤 <b>Foydalanuvchi:</b> {user_row['real_name']}\n\n❌ Hozircha siz uchun ochiq testlar yoki tayyor natijalar mavjud emas."
        
    if uid in ADMIN_IDS: text += "\n\n👨‍🏫 <i>Siz adminsiz. Boshqaruv menyusini ko'rish uchun /admin ni bosing.</i>"
        
    return text, markup


# ============================================================
# ISM-FAMILIYA RO'YXATGA OLISH / TAHRIRLASH
# ============================================================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_only(update): return
    uid = update.effective_user.id
    text = update.message.text.strip()
    
    with database() as db:
        user_row = db.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
        
    if not user_row or uid in EDITING_NAMES:
        if len(text) < 3 or len(text) > 40:
            await update.message.reply_text("⚠️ Iltimos, haqiqiy ism-familiyangizni to'liq kiriting (3-40 belgi oralig'ida).")
            return
            
        with database() as db:
            if user_row:
                db.execute("UPDATE users SET real_name = ? WHERE user_id = ?", (text, uid))
                db.execute("UPDATE attempts SET full_name = ? WHERE user_id = ?", (text, uid))
            else:
                db.execute("INSERT INTO users (user_id, real_name) VALUES (?, ?)", (uid, text))
                
        if uid in EDITING_NAMES:
            EDITING_NAMES.remove(uid)
            
        await update.message.reply_text(f"✅ Qabul qilindi, <b>{text}</b>!", parse_mode="HTML")
        text_msg, markup = get_main_menu(uid)
        await update.message.reply_text(text_msg, reply_markup=markup, parse_mode="HTML")
    else:
        if uid not in ADMIN_IDS:
            await update.message.reply_text("Bosh menyuni ko'rish uchun /start ni bosing.")


# ============================================================
# O'QUVCHI UCHUN START MENYUSI
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_only(update): return
    uid = update.effective_user.id
    text_msg, markup = get_main_menu(uid)
    await update.message.reply_text(text_msg, reply_markup=markup, parse_mode="HTML")


# ============================================================
# O'QUVCHI VA O'QITUVCHI TUGMALARI BOSHQRUVI
# ============================================================

async def send_question_ui(message_or_query, test_id, user_id, q_idx):
    test = get_test(test_id)
    attempt = get_attempt(test_id, user_id)
    questions = questions_of(test)
    answers = json.loads(attempt["answers_json"])
    current_ans = answers[q_idx]
    
    text = (f"📊 <b>{test['title']}</b>\n\nSavol: <b>{q_idx + 1} / {len(questions)}</b>\nSizning javobingiz: <b>{current_ans}</b>")
    
    keyboard = [
        [InlineKeyboardButton(f"A {'✅' if current_ans=='A' else ''}", callback_data=f"ans_{test_id}_{q_idx}_A")],
        [InlineKeyboardButton(f"B {'✅' if current_ans=='B' else ''}", callback_data=f"ans_{test_id}_{q_idx}_B")],
        [InlineKeyboardButton(f"C {'✅' if current_ans=='C' else ''}", callback_data=f"ans_{test_id}_{q_idx}_C")],
        [InlineKeyboardButton(f"D {'✅' if current_ans=='D' else ''}", callback_data=f"ans_{test_id}_{q_idx}_D")]
    ]
    nav_row = []
    if q_idx > 0: nav_row.append(InlineKeyboardButton("⬅️ Oldingi", callback_data=f"nav_{test_id}_{q_idx-1}"))
    if q_idx < len(questions) - 1: nav_row.append(InlineKeyboardButton("Keyingi ➡️", callback_data=f"nav_{test_id}_{q_idx+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("🏁 Yakunlash", callback_data=f"fin_{test_id}")])
    
    markup = InlineKeyboardMarkup(keyboard)
    if hasattr(message_or_query, 'edit_message_text'):
        await message_or_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await message_or_query.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def inline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    uid = user.id
    data = query.data
    
    if data == "editname":
        EDITING_NAMES.add(uid)
        await query.message.reply_text("✏️ Iltimos, yangi ism-familiyangizni kiriting (Masalan: Elshodbek Xolmatov):")
        try: await query.answer()
        except: pass
        return

    if data == "myresults":
        with database() as db:
            scored_tests = db.execute(
                """SELECT t.id, t.title, a.submitted_at 
                   FROM attempts a JOIN tests t ON a.test_id = t.id 
                   WHERE a.user_id = ? AND t.status = 'scored' AND a.result_json IS NOT NULL
                   ORDER BY a.submitted_at DESC LIMIT 30""", (uid,)
            ).fetchall()
        
        keyboard = []
        for s in scored_tests:
            dt = datetime.fromtimestamp(s['submitted_at']).strftime('%d.%m.%Y')
            keyboard.append([InlineKeyboardButton(f"📊 {s['title']} ({dt})", callback_data=f"result_{s['id']}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Orqaga", callback_data="backmain")])
        await query.edit_message_text("📊 <b>Sizning natijalaringiz:</b>\n\nQaysi test natijasini ko'rmoqchisiz?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        try: await query.answer()
        except: pass
        return
        
    if data == "backmain":
        text_msg, markup = get_main_menu(uid)
        await query.edit_message_text(text_msg, reply_markup=markup, parse_mode="HTML")
        try: await query.answer()
        except: pass
        return

    parts = data.split("_")
    action = parts[0]
    test_id = int(parts[1])
    
    if action == "adminstop":
        if uid not in ADMIN_IDS:
            await query.answer("Bu tugma faqat admin uchun!", show_alert=True)
            return
            
        test = get_test(test_id)
        if test["status"] != "open":
            await query.answer("Test hozir ochiq emas!", show_alert=True)
            return
            
        await query.edit_message_text("⏳ Test to'xtatildi! Natijalar hisoblanmoqda, Diplomlar yasalmoqda va o'quvchilarga yuborilmoqda...")
        
        with database() as db:
            db.execute("UPDATE tests SET status = 'scored' WHERE id = ?", (test_id,))
            attempts = db.execute("SELECT * FROM attempts WHERE test_id = ?", (test_id,)).fetchall()
            
        valid_attempts = [a for a in attempts if json.loads(a["answers_json"])]
        
        if not valid_attempts:
            await query.edit_message_text(f"✅ {test_id}-test to'xtatildi, ammo hech kim javob yo'llamagan.")
            return
            
        questions = questions_of(test)
        try:
            results, calibration = calculate_results(questions, valid_attempts)
        except Exception as e:
            await query.message.reply_text(f"⚠️ Hisoblashda xatolik yuz berdi: {e}")
            return
            
        with database() as db:
            db.execute("UPDATE tests SET calibration_json = ? WHERE id = ?", (json.dumps(calibration, ensure_ascii=False), test_id))
            for att_id, res in results.items():
                db.execute("UPDATE attempts SET result_json = ? WHERE id = ?", (json.dumps(res, ensure_ascii=False), att_id))
                
        scored_users = []
        for attempt in valid_attempts:
            res = results[attempt["id"]]
            mock_score = round(res["mock_score"], 2)
            grade = get_grade(mock_score)
            scored_users.append({
                "user_id": attempt["user_id"],
                "name": attempt["full_name"],
                "correct": res["correct"],
                "total": res["total"],
                "percent": res["percent"],
                "score": mock_score,
                "grade": grade,
                "time": attempt["submitted_at"]
            })
            
        scored_users.sort(key=lambda x: x["score"], reverse=True)
        
        sent_count = 0
        error_logs = []
        
        for i, su in enumerate(scored_users, 1):
            text_msg = (
                f"🔔 <b>DIQQAT! TEST YAKUNLANDI!</b>\n\n"
                f"📊 <b>{test['title']} Natijangiz:</b>\n\n"
                f"✅ To'g'ri javoblar: {su['correct']} / {su['total']}\n"
                f"📈 Foiz: {su['percent']:.1f}%\n"
                f"🎯 Ball (100 dan): {su['score']:.2f}\n"
                f"🎓 Daraja: <b>{su['grade']}</b>\n\n"
                f"🏆 <b>Sizning o'rningiz: {i}</b>"
            )
            try:
                if i <= 3:
                    today_str = datetime.fromtimestamp(su["time"] if su["time"] else now()).strftime("%d.%m.%Y")
                    diplom_res = generate_diploma(su['name'], su['percent'], i, today_str)
                    
                    if isinstance(diplom_res, str):
                        await context.bot.send_message(chat_id=su['user_id'], text=text_msg, parse_mode="HTML")
                        if diplom_res == "NO_PILLOW": error_logs.append("⚠️ Pillow kutubxonasi o'rnatilmagan.")
                        else: error_logs.append(f"⚠️ {su['name']} ning diplomida xatolik: {diplom_res}")
                    else:
                        await context.bot.send_photo(chat_id=su['user_id'], photo=diplom_res, caption=text_msg, parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id=su['user_id'], text=text_msg, parse_mode="HTML")
                sent_count += 1
            except:
                pass 

        table_lines = [f"🏆 <b>UMUMIY NATIJALAR JADVALI</b>\n📝 <b>Test:</b> {test['title']}\n"]
        for i, su in enumerate(scored_users, 1):
            g = su["grade"]
            if g in ["A+", "A"]: color = "🟢"
            elif g in ["B+", "B"]: color = "🟡"
            elif g in ["C+", "C"]: color = "🟠"
            else: color = "🔴"
            
            if i == 1: medal = "🥇"
            elif i == 2: medal = "🥈"
            elif i == 3: medal = "🥉"
            else: medal = f"<b>{i}.</b>"
            
            table_lines.append(f"{medal} {su['name']} — {color} <b>{g}</b> <i>({su['score']} ball | {su['correct']} ta)</i>")
            
        messages_to_send = []
        current_msg = ""
        for line in table_lines:
            if len(current_msg) + len(line) > 3800:
                messages_to_send.append(current_msg)
                current_msg = line + "\n"
            else:
                current_msg += line + "\n"
        if current_msg:
            messages_to_send.append(current_msg)
            
        for msg in messages_to_send:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
            
        error_report = "\n".join(list(set(error_logs)))
        if error_report:
            await context.bot.send_message(chat_id=uid, text=f"🔧 <b>TEXNIK XISOBOT (DIPLOM)</b>:\n\n{error_report}", parse_mode="HTML")

        await query.message.reply_text(
            f"✅ Barchasi tayyor! Natijalar o'quvchilarga bittalab yetkazildi.\n\n"
            f"Barcha natijalarni Excel ro'yxatida olish uchun bosing: /export {test_id}"
        )
        return

    test = get_test(test_id)
    attempt = get_attempt(test_id, uid)
    
    if action == "join":
        with database() as db:
            user_row = db.execute("SELECT real_name FROM users WHERE user_id = ?", (uid,)).fetchone()
            
        if not user_row:
            await query.answer("Ism-familiyangiz kiritilmagan!", show_alert=True)
            return
            
        real_name = user_row["real_name"]
        
        if not test or test["status"] != "open":
            await query.answer("Bu test yopilgan yoki topilmadi!", show_alert=True)
            return
            
        questions = questions_of(test)
        if not attempt:
            started = now()
            deadline = started + test["duration_minutes"] * 60
            empty_answers = ["-"] * len(questions)
            with database() as db:
                db.execute("INSERT INTO attempts (test_id, user_id, full_name, username, started_at, deadline, answers_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (test_id, uid, real_name, user.username, started, deadline, json.dumps(empty_answers)))
            
            await query.message.reply_text(f"✅ <b>{test['title']}</b> testiga qo'shildingiz!\n⏳ Vaqtingiz: {test['duration_minutes']} daqiqa.", parse_mode="HTML")
            if test["pdf_file_id"]: await query.message.reply_document(test["pdf_file_id"])
            await send_question_ui(query.message, test_id, uid, 0)
        else:
            qolgan_vaqt = max(0, (attempt["deadline"] - now()) // 60)
            if qolgan_vaqt > 0 and not attempt["submitted_at"]:
                await query.message.reply_text(f"Siz bu testga qo'shilgansiz. Qolgan vaqt: {qolgan_vaqt} daqiqa.")
                if test["pdf_file_id"]: await query.message.reply_document(test["pdf_file_id"])
                await send_question_ui(query.message, test_id, uid, 0)
            else:
                await query.answer("Test vaqti tugagan yoki yakunlagansiz!", show_alert=True)

    elif action == "result":
        if not test or test["status"] != "scored" or not attempt or not attempt["result_json"]:
            await query.answer("Natijalar hali e'lon qilinmagan!", show_alert=True)
            return
            
        res = json.loads(attempt["result_json"])
        mock_score = round(res.get('mock_score', 0), 2)
        grade = get_grade(mock_score)
        
        with database() as db:
            all_attempts = db.execute("SELECT result_json FROM attempts WHERE test_id = ? AND result_json IS NOT NULL", (test_id,)).fetchall()
        
        all_scores = []
        for a in all_attempts:
            r = json.loads(a["result_json"])
            all_scores.append(round(r.get("mock_score", 0), 2))
        all_scores.sort(reverse=True)
        
        try: place = all_scores.index(mock_score) + 1
        except: place = 0
            
        text = (f"📊 <b>{test['title']} Natijasi:</b>\n\n✅ To'g'ri javoblar: {res['correct']} / {res['total']}\n"
                f"📈 Foiz: {res['percent']:.1f}%\n🎯 Ball (100 dan): {mock_score:.2f}\n🎓 Daraja: <b>{grade}</b>\n\n"
                f"🏆 <b>Sizning o'rningiz: {place}</b>\n")
        
        keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="myresults")]]
        
        try: await query.message.delete()
        except: pass
            
        if place in [1, 2, 3]:
            today_str = datetime.fromtimestamp(attempt['submitted_at']).strftime("%d.%m.%Y")
            diplom_res = generate_diploma(attempt['full_name'], res['percent'], place, today_str)
            
            if isinstance(diplom_res, str):
                await context.bot.send_message(chat_id=uid, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            else:
                await context.bot.send_photo(chat_id=uid, photo=diplom_res, caption=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id=uid, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif action in ["ans", "nav", "fin"]:
        if not attempt or attempt["submitted_at"] or test["status"] != "open":
            await query.answer("Test yopilgan yoki allaqachon yakunlagansiz!", show_alert=True)
            return
        if now() > attempt["deadline"]:
            await query.answer("Vaqtingiz tugagan!", show_alert=True)
            return
            
        if action == "ans":
            q_idx = int(parts[2])
            ans = parts[3]
            answers = json.loads(attempt["answers_json"])
            answers[q_idx] = ans
            with database() as db:
                db.execute("UPDATE attempts SET answers_json = ? WHERE id = ?", (json.dumps(answers), attempt["id"]))
            
            questions = questions_of(test)
            next_idx = q_idx + 1 if q_idx < len(questions) - 1 else q_idx
            await send_question_ui(query, test_id, uid, next_idx)
            
        elif action == "nav":
            q_idx = int(parts[2])
            await send_question_ui(query, test_id, uid, q_idx)
            
        elif action == "fin":
            with database() as db:
                db.execute("UPDATE attempts SET submitted_at = ? WHERE test_id = ? AND user_id = ?", (now(), test_id, uid))
            
            text_msg, markup = get_main_menu(uid)
            await query.edit_message_text(
                "✅ <b>Javoblaringiz muvaffaqiyatli topshirildi!</b>\n\nO'qituvchi testni yakunlagach natijangiz shu yerga avtomatik keladi.\n\n" + text_msg, 
                reply_markup=markup,
                parse_mode="HTML"
            )
            
    try: await query.answer()
    except: pass


# ============================================================
# O'QITUVCHI FUNKSIYALARI
# ============================================================

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update): return
    await update.message.reply_text(
        "⚙️ O'qituvchi Boshqaruvi\n\n"
        "1. Yangi test: /new 120 Ona tili ABCDABCD\n"
        "2. PDF qo'shish: PDF yuboring, izohiga /pdf 1 yozing\n"
        "3. Testni ochish: /open 1\n\n"
        "📋 Natijalarni ko'rish uchun: /export 1"
    )

async def create_test_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update): return
    if len(context.args) < 2:
        await update.message.reply_text("Xato! /new [vaqt] [nomi] [kalitlar]\nMisol: /new 120 Ona tili ABCD")
        return
        
    try: duration = int(context.args[0])
    except:
        await update.message.reply_text("Vaqtni (daqiqa) birinchi yozing.")
        return
        
    keys_raw = context.args[-1].upper()
    if not all(k in "ABCD" for k in keys_raw):
        await update.message.reply_text("Javoblar (kalitlar) faqat A, B, C, D dan iborat bo'lsin.")
        return
        
    title = " ".join(context.args[1:-1]) if len(context.args) > 2 else f"Test {now()}"
    questions = [{"key": k, "options": "ABCD", "topic": "Umumiy"} for k in keys_raw]
    
    with database() as db:
        cursor = db.execute("INSERT INTO tests(title, duration_minutes, questions_json, created_at) VALUES (?, ?, ?, ?)",
                            (title, duration, json.dumps(questions, ensure_ascii=False), now()))
        test_id = cursor.lastrowid
        
    await update.message.reply_text(f"✅ Test yaratildi!\n\nID: {test_id}\nNomi: {title}\nSavollar: {len(questions)}\n\nEndi PDF ni yuboring, izohiga yozing: /pdf {test_id}")

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update): return
    document = update.message.document
    caption = (update.message.caption or "").strip()

    if document.file_name and document.file_name.lower().endswith(".pdf"):
        match = re.fullmatch(r"/pdf(?:@\w+)?\s+(\d+)", caption)
        if not match:
            await update.message.reply_text("⚠️ Xato: PDF izohiga /pdf ID yozing. Masalan: /pdf 1")
            return
        test_id = int(match.group(1))
        test = get_test(test_id)
        if not test: 
            await update.message.reply_text("⚠️ Bunday test topilmadi.")
            return

        with database() as db:
            db.execute("UPDATE tests SET pdf_file_id = ? WHERE id = ?", (document.file_id, test_id))
        await update.message.reply_text(f"✅ PDF {test_id}-testga biriktirildi.\nEndi testni ochish uchun: /open {test_id}")

async def open_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update): return
    try: test_id = command_id(context)
    except Exception as e:
        await update.message.reply_text(f"⚠️ {str(e)}\nMisol: /open 1")
        return
        
    test = get_test(test_id)
    if not test: 
        await update.message.reply_text("Test topilmadi.")
        return
        
    with database() as db:
        db.execute("UPDATE tests SET status = 'open', opened_at = ? WHERE id = ?", (now(), test_id))
        
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Testni to'xtatish va Natijalarni Tarqatish", callback_data=f"adminstop_{test_id}")]])
    await update.message.reply_text(f"✅ {test_id}-test ochildi.\nO'quvchilar /start orqali kirishlari mumkin.", reply_markup=markup)

async def export_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update): return
    test_id = command_id(context)
    with database() as db:
        attempts = db.execute("SELECT * FROM attempts WHERE test_id = ?", (test_id,)).fetchall()

    output = []
    for attempt in attempts:
        if not attempt["result_json"]: continue
        result = json.loads(attempt["result_json"])
        mock_score = round(result.get("mock_score", 0), 2)
        output.append([attempt["full_name"], attempt["username"] or "", result["correct"], result["percent"], mock_score, get_grade(mock_score)])

    output.sort(key=lambda row: row[4], reverse=True)
    await send_csv(update=update, filename=f"test_{test_id}.csv", header=["Ism", "Username", "To'g'ri", "Foiz", "Ball", "Daraja"], rows=output)

async def all_tests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update): return
    with database() as db:
        rows = db.execute("SELECT * FROM tests ORDER BY id DESC LIMIT 60").fetchall()
    await update.message.reply_text("\n".join(f"ID: {row['id']} | Status: {row['status']} | Nom: {row['title']}" for row in rows) or "Testlar yo'q.")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(inline_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("new", create_test_text))
    app.add_handler(CommandHandler("alltests", all_tests))
    app.add_handler(CommandHandler("open", open_test))
    app.add_handler(CommandHandler("export", export_results))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_document))
    app.run_polling()

if __name__ == "__main__":
    main()
