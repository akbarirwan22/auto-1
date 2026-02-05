#!/usr/bin/env python3
"""
Telegram Bot untuk Scraping Stok Emas Antam
Fitur:
- Auto-scan tiap menit during 10-min window sebelum war times
- Manual scan via command
- Inline keyboard buttons untuk better UX
- Detail stok per cabang
"""

import asyncio
import logging
import json
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# Import fungsi dari waremas.py
import os
import sys
# Add parent dir to path to import waremas
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from waremas import (
    load_config,
    make_scraper,
    scan_all_cabang,
    CABANG_LIST,
    ensure_bootstrap_cf_and_session,
    cookiejar_to_header,
    COOKIE_DOMAIN,
    login_account,
    save_cookies,
    ACCOUNTS
)

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Suppress httpx logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Timezone
TZ = pytz.timezone('Asia/Jakarta')

# War times (setiap 15 menit dari 07:00 - 14:30)
WAR_TIMES = [
    "07:00", "07:15", "07:30", "07:45",
    "08:00", "08:15", "08:30", "08:45",
    "09:00", "09:15", "09:30", "09:45",
    "10:00", "10:15", "10:30", "10:45",
    "11:00", "11:15", "11:30", "11:45",
    "12:00", "12:15", "12:30", "12:45",
    "13:00", "13:15", "13:30", "13:45",
    "14:00", "14:15", "14:30"
]

# Global state
AUTO_SCAN_ENABLED = True
LIVE_MONITOR_ENABLED = False  # New feature
CHAT_ID = None  # Will be set from config
LAST_KEYBOARD_MESSAGE_ID = {}   # Store last message ID per chat_id for editing


def get_next_war_time() -> Optional[str]:
    """Get next upcoming war time (GENERAL - actual times vary per branch!)"""
    now = datetime.now(TZ)
    current_time = now.time()
    
    # NOTE: These are GENERAL times only!
    # Actual war times VARY PER BRANCH (e.g., Djuanda: 06:00, others: 07:00)
    # Always check scan results for accurate war time per branch
    for war_time_str in WAR_TIMES:
        war_time = datetime.strptime(war_time_str, "%H:%M").time()
        if war_time > current_time:
            return war_time_str
    
    # If no more today, return first war time tomorrow
    return WAR_TIMES[0]


def format_stock_message(scan_results: List[Dict], scan_time: str) -> str:
    """Format scan results into beautiful Telegram message"""
    
    # Count available branches
    available_count = sum(1 for r in scan_results if r.get('status') == 'success' and r.get('sesi') != '-')
    total_count = len(scan_results)
    
    message = f"🔍 <b>STOCK UPDATE - {scan_time} WIB</b>\n"
    message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Show available branches first
    available_branches = [r for r in scan_results if r.get('status') == 'success' and r.get('sesi') != '-']
    unavailable_branches = [r for r in scan_results if not (r.get('status') == 'success' and r.get('sesi') != '-')]
    
    # Available branches
    for result in available_branches:
        cabang = result.get('cabang', 'Unknown')
        sesi = result.get('sesi', '-')
        waktu1 = result.get('waktu1', '-')
        waktu2 = result.get('waktu2', '-')
        
        message += f"✅ <b>{cabang}</b>\n"
        message += f"   • Sesi: {sesi}\n"
        
        if waktu1 != '-':
            message += f"   • {waktu1}\n"
        if waktu2 != '-':
            message += f"   • {waktu2}\n"
        
        message += "\n"
    
    # Unavailable branches (collapsed)
    if unavailable_branches:
        message += f"❌ <b>Tidak Tersedia ({len(unavailable_branches)} cabang)</b>\n"
        unavailable_names = [r.get('cabang', 'Unknown') for r in unavailable_branches[:5]]
        if len(unavailable_branches) > 5:
            message += f"   {', '.join(unavailable_names)}, dan {len(unavailable_branches) - 5} lainnya\n"
        else:
            message += f"   {', '.join(unavailable_names)}\n"
        message += "\n"
    
    message += "━━━━━━━━━━━━━━━━━━━━━━\n"
    message += f"⏰ Next War: {get_next_war_time()} WIB\n"
    message += f"📊 Total Available: {available_count}/{total_count} cabang"
    
    return message


def auto_login_if_needed() -> bool:
    """Auto-login using first account from akun.txt if cookies expired"""
    try:
        if not ACCOUNTS:
            logger.error("No accounts found in akun.txt")
            return False
        
        # Use first account
        account = ACCOUNTS[0]
        logger.info(f"🔐 Auto-logging in as {account['email']}...")
        
        # Call waremas login function
        success = login_account(account)
        
        if success:
            logger.info("✅ Auto-login successful!")
            return True
        else:
            logger.error("❌ Auto-login failed")
            return False
            
    except Exception as e:
        logger.error(f"Auto-login error: {e}")
        import traceback
        traceback.print_exc()
        return False


def load_cookies_from_file(cookie_file='cookies.json'):
    """Load cookies from waremas.py format (cookies.json)"""
    if not os.path.exists(cookie_file):
        logger.error(f"Cookie file {cookie_file} not found")
        return {}
    
    with open(cookie_file, 'r') as f:
        cookies_data = json.load(f)
    
    # cookies.json format:
    # {"phone": {"0": {"csrf_cookie_name": "...", "ci_session": "..."}}}
    
    # Extract first phone number's first session
    for phone, sessions in cookies_data.items():
        if '0' in sessions:
            return sessions['0']
    
    return {}


def format_scan_results_new(results: List[Dict]) -> str:
    """Format waremas.py scan results for Telegram message"""
    if not results:
        return "❌ Scan failed - No results"
    
    # 1. Parse time for sorting
    for r in results:
        # Extract HH:MM from mulaiLabel (e.g. "09:00-10:00" -> "09:00")
        label = r.get('mulaiLabel', '-')
        r['sort_time'] = "23:59" # Default late
        try:
            if label and label != '-':
                # Simple extraction: take first 5 chars if they look like time
                import re
                m = re.search(r"(\d{2}:\d{2})", label)
                if m:
                    r['sort_time'] = m.group(1)
        except:
            pass

    # Group by status: READY, PENUH, KOSONG
    # Sort READY by time
    ready = sorted(
        [r for r in results if r.get('status') == 'READY'],
        key=lambda x: x['sort_time']
    )
    penuh = [r for r in results if r.get('status') == 'PENUH']
    kosong = [r for r in results if r.get('status') not in ['READY', 'PENUH']]
    
    now = datetime.now(TZ)
    scan_time = now.strftime("%H:%M:%S")
    
    # Calculate Next War from earliest READY branch
    next_war_display = "-"
    if ready:
        next_war_display = f"{ready[0]['sort_time']} WIB"
    else:
        # Fallback to general schedule if no ready branches
        next_war_display = f"{get_next_war_time()} WIB (Est)"

    message = f"🔍 <b>STOCK UPDATE - {scan_time} WIB</b>\n"
    
    if ready:
        message += f"\n✅ <b>STOK TERSEDIA ({len(ready)} cabang)</b>"
        
        for r in ready:
            nama = r.get('nama', 'Unknown').upper()
            gramasi = r.get('gramasi', '-')
            aktif = r.get('aktifCount', 0)
            sisa = r.get('sisaKuota', '-')
            war_time = r.get('mulaiLabel', '-')  # War time / session label
            
            message += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"<b>{nama}</b>\n"
            message += f"📦 {gramasi}\n"
            message += f"🎫 {aktif} slots | Sisa: {sisa}\n"
            message += f"⏰ {war_time}"
        
        message += "\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    else:
        message += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        message += "❌ <b>TIDAK ADA STOK TERSEDIA</b>\n"
        message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if penuh:
        message += f"⚠️ <b>PENUH ({len(penuh)} cabang)</b>:\n"
        # Sort vertically compact
        penuh_names = [f"• {r.get('nama', 'Unknown')}" for r in penuh]
        # Show max 5, then summary
        if len(penuh_names) > 5:
            message += "\n".join(penuh_names[:5])
            message += f"\n... dan {len(penuh) - 5} lainnya\n"
        else:
            message += "\n".join(penuh_names)
        message += "\n\n"
    
    # Kosong/Hidden usually not needed in detail, keep clean
    # if kosong:
    #    message += f"❌ <b>KOSONG/N/A ({len(kosong)} cabang)</b>\n"
    
    message += f"⏰ Next War: {next_war_display} | 📊 Avail: {len(ready)}/{len(results)}"
    
    return message


async def scan_all_branches() -> List[Dict]:
    """Scan all branches with retry logic for session expiry"""
    max_retries = 3
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🔍 Starting scan (attempt {attempt}/{max_retries})...")
            
            # Check if we have accounts loaded
            if not ACCOUNTS:
                logger.error("❌ No accounts loaded from akun.txt")
                return []
            
            account = ACCOUNTS[0]
            
            # Check if account has active session
            if "session" not in account or not account.get("session"):
                logger.warning("⚠️ No active session found, attempting login...")
                success = login_account(account)
                if not success:
                    logger.error(f"❌ Login failed (attempt {attempt}/{max_retries})")
                    if attempt < max_retries:
                        logger.info(f"⏳ Retrying in 2 seconds...")
                        await asyncio.sleep(2)
                        continue
                    return []
            
            # Get session from account (waremas stores it here after login)
            session = account.get("session")
            if not session:
                logger.error("❌ No session available after login")
                if attempt < max_retries:
                    logger.info(f"⏳ Retrying in 2 seconds...")
                    await asyncio.sleep(2)
                    continue
                return []
            
            logger.info(f"✅ Using session for {account['email']}")
            
            # Call waremas scan function directly with the active session
            logger.info(f"🔍 Scanning {len(CABANG_LIST)} branches...")
            
            # CRITICAL FIX: Run synchronous blocking scan in executor to avoid freezing the bot
            loop = asyncio.get_running_loop()
            from functools import partial
            
            # Use default executor (Thread pool)
            results = await loop.run_in_executor(
                None, 
                partial(scan_all_cabang, session, CABANG_LIST)
            )
            
            # Check if results is empty due to session expiry
            if not results:
                # Check if it's due to redirect/session expired
                logger.warning("⚠️ Scan returned empty results")
                
                # Clear session and retry with fresh login
                if "session" in account:
                    del account["session"]
                
                if attempt < max_retries:
                    logger.info(f"🔄 Session might be expired, retrying with fresh login...")
                    await asyncio.sleep(1)
                    continue
                    
                return []
            
            logger.info(f"✅ Scan completed: {len(results)} branches scanned")
            return results
            
        except Exception as e:
            logger.error(f"❌ Scan error (attempt {attempt}/{max_retries}): {e}")
            import traceback
            traceback.print_exc()
            
            # Clear session on error
            if "session" in account:
                del account["session"]
            
            if attempt < max_retries:
                logger.info(f"⏳ Retrying in 2 seconds...")
                await asyncio.sleep(2)
                continue
            
            return []
    
    logger.error(f"❌ All {max_retries} scan attempts failed")
    return []


async def send_or_update_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, markup: InlineKeyboardMarkup = None):
    """
    Smart message sender:
    - Tries to edit the LAST known message ID for this chat.
    - If it fails (deleted or too old), sends a NEW message.
    - Updates LAST_KEYBOARD_MESSAGE_ID.
    """
    global LAST_KEYBOARD_MESSAGE_ID
    
    last_msg_id = LAST_KEYBOARD_MESSAGE_ID.get(chat_id)
    sent_msg = None
    
    if last_msg_id:
        try:
            # Try to edit existing message
            sent_msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=last_msg_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"✏️ Updated message {last_msg_id}")
            return  # Success, exit
            
        except Exception as e:
            # If "Message is not modified" (content same), ignore
            if "Message is not modified" in str(e):
                logger.info("ℹ️ Message content unchanged, skipping update")
                return
            
            # If any other error (message deleted, too old, etc), proceed to send new one
            logger.warning(f"⚠️ Could not edit message {last_msg_id}: {e}")
            pass

    # Send NEW message
    try:
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )
        # Update stored ID
        LAST_KEYBOARD_MESSAGE_ID[chat_id] = sent_msg.message_id
        logger.info(f"📤 Sent new message {sent_msg.message_id}")
        
    except Exception as e:
        logger.error(f"❌ Failed to send message: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    global LAST_KEYBOARD_MESSAGE_ID
    
    live_emoji = "🟢" if LIVE_MONITOR_ENABLED else "🔴"
    
    keyboard = [
        [InlineKeyboardButton("🔍 Scan Sekarang", callback_data="scan_now")],
        [InlineKeyboardButton(f"{live_emoji} Live Monitor (Auto-Refresh)", callback_data="toggle_live_monitor")],
        [InlineKeyboardButton("📊 Lihat Jadwal War", callback_data="show_schedule")],
        [InlineKeyboardButton("⚙️ Pengaturan Auto-Scan", callback_data="auto_scan_settings")],
        [InlineKeyboardButton("ℹ️ Help & Info", callback_data="show_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "🔱 <b>BOT ANTRIAN LOGAM MULIA</b> 🔱\n\n"
        "Bot ini akan membantu kamu untuk:\n"
        "✅ Auto-scan stok tiap menit sebelum war\n"
        "✅ Notifikasi real-time cabang tersedia\n"
        "✅ Manual scan kapan aja\n\n"
        "Pilih menu di bawah untuk mulai:"
    )
    
    sent_msg = await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    
    # Store this message ID to enable editing later
    if update.effective_chat.id:
        LAST_KEYBOARD_MESSAGE_ID[update.effective_chat.id] = sent_msg.message_id


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan command"""
    msg = await update.message.reply_text("🔄 Scanning all branches...\nMohon tunggu ~15 detik...")
    
    # Perform scan
    results = await scan_all_branches()
    
    if not results:
        await msg.edit_text("❌ Scan gagal! Silakan coba lagi.")
        return
    
    # Format and send message
    scan_time = datetime.now(TZ).strftime("%H:%M:%S")
    message = format_scan_results_new(results)
    
    # Add refresh button
    keyboard = [[InlineKeyboardButton("🔄 Refresh Data", callback_data="scan_now")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Update the wait message with results
    final_msg = await msg.edit_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    
    # Update tracked message ID
    if update.effective_chat.id:
        LAST_KEYBOARD_MESSAGE_ID[update.effective_chat.id] = final_msg.message_id


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks"""
    global AUTO_SCAN_ENABLED, LIVE_MONITOR_ENABLED, LAST_KEYBOARD_MESSAGE_ID
    
    query = update.callback_query
    chat_id = query.message.chat.id
    
    # Update tracked message ID since user interacted with this one
    LAST_KEYBOARD_MESSAGE_ID[chat_id] = query.message.message_id
    
    await query.answer()
    
    if query.data == "scan_now":
        # Don't delete entire message, just change text to indicate loading
        await query.edit_message_text("🔄 Scanning all branches...\nMohon tunggu ~15 detik...")
        
        # Perform scan
        results = await scan_all_branches()
        
        # Determine keyboard based on mode
        if LIVE_MONITOR_ENABLED:
            keyboard = [
                 [InlineKeyboardButton("🔄 Refresh Data", callback_data="scan_now")],
                 [
                     InlineKeyboardButton("🔴 Matikan", callback_data="toggle_live_monitor"),
                     InlineKeyboardButton("◀️ Menu", callback_data="back_to_menu")
                 ]
            ]
        else:
             keyboard = [
                 [InlineKeyboardButton("🔄 Refresh Data", callback_data="scan_now")],
                 [InlineKeyboardButton("◀️ Kembali ke Menu", callback_data="back_to_menu")]
            ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if not results:
            # If scan failed, show error but keep the menu accessible
            await query.edit_message_text(
                "❌ Scan gagal! Silakan coba lagi.",
                reply_markup=reply_markup
            )
            return
        
        # Format and send message
        scan_time = datetime.now(TZ).strftime("%H:%M:%S")
        message = format_scan_results_new(results)
        
        if LIVE_MONITOR_ENABLED:
            message = "🟢 <b>LIVE MONITOR RUNNING</b>\n" + message
        
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

    elif query.data == "toggle_live_monitor":
        LIVE_MONITOR_ENABLED = not LIVE_MONITOR_ENABLED
        job_queue = context.job_queue
        
        if LIVE_MONITOR_ENABLED:
            # Start Live Monitor Job
            # Check if already running
            current_jobs = job_queue.get_jobs_by_name("live_monitor_job")
            if not current_jobs:
                job_queue.run_repeating(live_monitor_job, interval=60, first=1, chat_id=chat_id, name="live_monitor_job")
                logger.info(f"🟢 Live Monitor STARTED for chat {chat_id}")
            
            status_text = "🟢 <b>LIVE MONITOR: ON</b>"
            desc_text = "Bot akan refresh data otomatis setiap 60 detik.\nTekan tombol di bawah untuk mematikan."
             
        else:
            # Stop Live Monitor Job
            current_jobs = job_queue.get_jobs_by_name("live_monitor_job")
            for job in current_jobs:
                job.schedule_removal()
            logger.info(f"🔴 Live Monitor STOPPED for chat {chat_id}")
            
            status_text = "🔴 <b>LIVE MONITOR: OFF</b>"
            desc_text = "Auto-refresh dimatikan.\nTekan tombol di bawah untuk mengaktifkan."
            
        
        # Show updated status
        keyboard = [
            [InlineKeyboardButton(
                f"{'🔴 Matikan' if LIVE_MONITOR_ENABLED else '🟢 Aktifkan'} Live Monitor",
                callback_data="toggle_live_monitor"
            )],
             [InlineKeyboardButton("◀️ Kembali ke Menu", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"{status_text}\n\n{desc_text}",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif query.data == "show_schedule":
        schedule_text = "📅 <b>JADWAL WAR TIMES</b>\n\n"
        schedule_text += "⚠️ <b>PENTING:</b> Jam war <b>BERBEDA per cabang!</b>\n"
        schedule_text += "Contoh: Djuanda buka 06:00, cabang lain 07:00, dll.\n\n"
        schedule_text += "📋 Jam umum (orientasi):\n"
        
        # Group by hour
        current_hour = None
        for war_time in WAR_TIMES:
            hour = war_time.split(":")[0]
            if hour != current_hour:
                schedule_text += f"\n<b>{hour}:xx</b> - "
                current_hour = hour
            schedule_text += f"{war_time} "
        
        schedule_text += f"\n\n⏰ Next War (umum): <b>{get_next_war_time()} WIB</b>"
        schedule_text += "\n\n💡 <b>Cara tau jam war per cabang:</b>"
        schedule_text += "\n   → Click 'Scan Sekarang'"
        schedule_text += "\n   → Liat di '⏰ War:' setiap cabang"
        schedule_text += "\n\n🤖 Auto-scan: 10 menit sebelum war time"
        
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            schedule_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif query.data == "auto_scan_settings":
        status_emoji = "✅" if AUTO_SCAN_ENABLED else "🔕"
        status_text = "ON" if AUTO_SCAN_ENABLED else "OFF"
        
        settings_text = f"⚙️ <b>PENGATURAN AUTO-SCAN (PRE-WAR)</b>\n\n"
        settings_text += f"Status: {status_emoji} <b>{status_text}</b>\n\n"
        
        if AUTO_SCAN_ENABLED:
            settings_text += "Bot akan otomatis scan tiap menit,\n10 menit sebelum setiap war time."
        else:
            settings_text += "Auto-scan dinonaktifkan.\nGunakan manual scan jika perlu."
        
        keyboard = [
            [InlineKeyboardButton(
                f"{'🔕 Matikan' if AUTO_SCAN_ENABLED else '✅ Aktifkan'} Auto-Scan",
                callback_data="toggle_auto_scan"
            )],
            [InlineKeyboardButton("◀️ Kembali", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            settings_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif query.data == "toggle_auto_scan":
        AUTO_SCAN_ENABLED = not AUTO_SCAN_ENABLED
        
        # Show updated settings instead of recursive callback
        status_emoji = "✅" if AUTO_SCAN_ENABLED else "🔕"
        status_text = "ON" if AUTO_SCAN_ENABLED else "OFF"
        
        settings_text = f"⚙️ <b>PENGATURAN AUTO-SCAN (PRE-WAR)</b>\n\n"
        settings_text += f"Status: {status_emoji} <b>{status_text}</b>\n\n"
        
        if AUTO_SCAN_ENABLED:
            settings_text += "Bot akan otomatis scan tiap menit,\n10 menit sebelum setiap war time."
        else:
            settings_text += "Auto-scan dinonaktifkan.\nGunakan manual scan jika perlu."
        
        keyboard = [
            [InlineKeyboardButton(
                f"{'🔕 Matikan' if AUTO_SCAN_ENABLED else '✅ Aktifkan'} Auto-Scan",
                callback_data="toggle_auto_scan"
            )],
            [InlineKeyboardButton("◀️ Kembali", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            settings_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif query.data == "show_help":
        help_text = (
            "ℹ️ <b>HELP & INFO</b>\n\n"
            "<b>Commands:</b>\n"
            "/start - Tampilkan menu utama\n"
            "/scan - Scan manual semua cabang\n\n"
            "<b>Fitur Live Monitor:</b>\n"
            "• Bot scan otomatis setiap 60 detik\n"
            "• Display stock update real-time tanpa spam\n\n"
             "<b>Fitur Pre-War Auto-Scan:</b>\n"
            "• Bot scan tiap menit (60 detik)\n"
            "• Dimulai 10 menit sebelum jadwal war\n\n"
            "<b>Tips:</b>\n"
            "💡 Gunakan Live Monitor jika ingin pantau terus\n"
            "💡 Matikan jika tidak digunakan agar hemat resource\n"
        )
        
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            help_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    
    elif query.data == "back_to_menu":
        # Auto-stop live monitor if going back to menu
        if LIVE_MONITOR_ENABLED:
            LIVE_MONITOR_ENABLED = False
            # Remove job
            current_jobs = context.job_queue.get_jobs_by_name("live_monitor_job")
            for job in current_jobs:
                job.schedule_removal()
            logger.info(f"🔴 Live Monitor STOPPED (User went back to menu) for chat {chat_id}")

        live_emoji = "🟢" if LIVE_MONITOR_ENABLED else "🔴"
        
        keyboard = [
            [InlineKeyboardButton("🔍 Scan Sekarang", callback_data="scan_now")],
            [InlineKeyboardButton(f"{live_emoji} Live Monitor (Auto-Refresh)", callback_data="toggle_live_monitor")],
            [InlineKeyboardButton("📊 Lihat Jadwal War", callback_data="show_schedule")],
            [InlineKeyboardButton("⚙️ Pengaturan Auto-Scan", callback_data="auto_scan_settings")],
            [InlineKeyboardButton("ℹ️ Help & Info", callback_data="show_help")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = (
            "🔱 <b>BOT ANTRIAN LOGAM MULIA</b> 🔱\n\n"
            "Bot ini akan membantu kamu untuk:\n"
            "✅ Auto-scan stok tiap menit sebelum war\n"
            "✅ Notifikasi real-time cabang tersedia\n"
            "✅ Manual scan kapan aja\n\n"
            "Pilih menu di bawah untuk mulai:"
        )
        
        await query.edit_message_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )


# Global state for change detection
PREVIOUS_SCAN_RESULTS = {}  # {chat_id: {branch_name: result_dict}}

def check_for_changes(chat_id: int, new_results: List[Dict]) -> List[str]:
    """Compare new results with previous ones and return list of alert messages"""
    alerts = []
    global PREVIOUS_SCAN_RESULTS
    
    # Initialize previous results for this chat if not exists
    if chat_id not in PREVIOUS_SCAN_RESULTS:
        # First run, just save state, no alerts (or maybe alert if found?)
        # Let's just save to avoid spamming on startup
        PREVIOUS_SCAN_RESULTS[chat_id] = {r['nama']: r for r in new_results}
        return []
    
    old_results_map = PREVIOUS_SCAN_RESULTS[chat_id]
    new_results_map = {r['nama']: r for r in new_results}
    
    for branch_name, new_data in new_results_map.items():
        old_data = old_results_map.get(branch_name)
        
        if not old_data:
            continue
            
        # Check 1: Status changed to READY/MENU from PENUH/N/A
        # status key might be 'status' or derived. In scan_all_cabang:
        # result = {'nama': ..., 'status': 'READY'/'PENUH'/..., ...}
        
        new_status = new_data.get('status', 'N/A')
        old_status = old_data.get('status', 'N/A')
        
        # Interest condition: becoming available
        is_now_available = new_status in ["READY", "MENU", "KOSONG"] # KOSONG = available 0 slots? No, default logical map
        # Based on waremas.py: 
        # READY (hijau) if aktifCount > 0
        # PENUH (merah) if aktifCount == 0
        # N/A (abu) if error/closed
        
        was_available = old_status in ["READY", "MENU"]
        
        # Trigger 1: Becomes available
        if new_status == "READY" and old_status != "READY":
            slots = new_data.get('aktifCount', 0)
            alerts.append(f"�🚨🚨 <b>{branch_name}</b> BUKA! ({slots} slot)")
            
        # Trigger 2: Slot count increased significantly (e.g. restock)
        # Only if already READY
        elif new_status == "READY" and old_status == "READY":
            new_slots = int(new_data.get('aktifCount', 0))
            old_slots = int(old_data.get('aktifCount', 0))
            
            if new_slots > old_slots:
                diff = new_slots - old_slots
                alerts.append(f"📈 <b>{branch_name}</b> NAMBAH {diff} SLOT! (Total: {new_slots})")

    # Update state
    PREVIOUS_SCAN_RESULTS[chat_id] = new_results_map
    return alerts


async def live_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JOB: Run scan every 60s and EDIT local message"""
    chat_id = context.job.chat_id
    
    logger.info(f"🔄 Live Monitor running for chat {chat_id}...")
    
    try:
        # Perform scan
        results = await scan_all_branches()
        
        # if not results:
        #    return  # OLD: Skip update if failed
        
        # Check for changes
        alerts = check_for_changes(chat_id, results)
        
        # If alerts found, send them as NEW messages (Notifications!)
        if alerts:
            for alert in alerts:
                await context.bot.send_message(chat_id=chat_id, text=alert, parse_mode=ParseMode.HTML)
        
        # Format message
        message = format_scan_results_new(results)
        message = "🟢 <b>LIVE MONITOR RUNNING</b>\n" + message
        
        # Add Refresh/Stop buttons - ADDED BACK TO MENU
        keyboard = [
             [InlineKeyboardButton("🔄 Refresh Data", callback_data="scan_now")],
             [
                 InlineKeyboardButton("🔴 Matikan", callback_data="toggle_live_monitor"),
                 InlineKeyboardButton("◀️ Menu", callback_data="back_to_menu")
             ]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        
        # Use smart update
        await send_or_update_message(context, chat_id, message, markup)
        
    except Exception as e:
        logger.error(f"Live Monitor error: {e}")
        import traceback
        traceback.print_exc()


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job for auto-scanning"""
    global AUTO_SCAN_ENABLED, CHAT_ID
    
    if not AUTO_SCAN_ENABLED:
        logger.info("Auto-scan disabled, skipping...")
        return
    
    if not CHAT_ID:
        logger.warning("No CHAT_ID configured, skipping auto-scan")
        return
    
    # If Live Monitor is already running, skip this to avoid double scan
    if LIVE_MONITOR_ENABLED:
        logger.info("Live Monitor active, skipping scheduled auto-scan")
        return
    
    logger.info("Running auto-scan...")
    
    try:
        # Perform scan
        results = await scan_all_branches()
        
        if not results:
            logger.error("Auto-scan failed")
            return
        
        # Format message
        scan_time = datetime.now(TZ).strftime("%H:%M:%S")
        message = format_scan_results_new(results)
        message = "🤖 <b>PRE-WAR AUTO-SCAN</b>\n\n" + message
        
        # Add refresh button
        keyboard = [[InlineKeyboardButton("🔄 Refresh Data", callback_data="scan_now")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send new message (since user might not be looking)
        # Or you can use send_or_update_message here too if you prefer
        # generally pre-war notifications imply we want to ALERT user, so new message is better?
        # But user asked to avoid spam. Let's use smart update for consistency!
        
        await send_or_update_message(context.bot, CHAT_ID, message, reply_markup)
        
        logger.info("Auto-scan complete and sent!")
        
    except Exception as e:
        logger.error(f"Error in auto_scan_job: {e}")
        import traceback
        traceback.print_exc()


def setup_schedule(application: Application) -> None:
    """Setup automatic scanning schedule"""
    job_queue = application.job_queue
    
    logger.info("Setting up auto-scan schedule...")
    
    # For each war time, schedule scans every minute for 10 minutes before
    for war_time_str in WAR_TIMES:
        war_dt = datetime.strptime(war_time_str, "%H:%M")
        
        # Schedule scan every minute from -10 to -1 minutes before war
        for offset in range(10, 0, -1):
            scan_time = war_dt - timedelta(minutes=offset)
            
            # Schedule daily at this time
            job_queue.run_daily(
                auto_scan_job,
                time=scan_time.time(),
                days=(0, 1, 2, 3, 4, 5, 6),  # All days
                name=f"auto_scan_{war_time_str}_{offset}min_before"
            )
            
            logger.info(f"Scheduled scan at {scan_time.strftime('%H:%M')} (for war {war_time_str})")
    
    logger.info(f"Total scheduled jobs: {len(WAR_TIMES) * 10}")


def main() -> None:
    """Start the bot"""
    global CHAT_ID
    
    # Load config
    config = load_config()
    if not config:
        logger.error("Failed to load config!")
        sys.exit(1)
    
    # Get Telegram credentials
    bot_token = config.get('telegramBotToken')
    chat_id = config.get('telegramChatId')
    
    if not bot_token:
        logger.error("❌ telegramBotToken not found in config!")
        logger.info("Please add 'telegramBotToken' to configemas.json")
        sys.exit(1)
    
    if not chat_id:
        logger.warning("⚠️ telegramChatId not found in config")
        logger.info("Auto-scan notifications will be disabled")
        logger.info("You can still use manual /scan command")
    else:
        CHAT_ID = chat_id
        logger.info(f"Chat ID configured: {CHAT_ID}")
    
    # Create application
    application = Application.builder().token(bot_token).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Setup auto-scan schedule
    setup_schedule(application)
    
    # Start bot
    logger.info("🤖 Bot started successfully!")
    logger.info("Press Ctrl+C to stop")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
