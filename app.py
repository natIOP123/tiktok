import asyncio
import logging
import time
import random
from datetime import datetime
from typing import Dict, Optional, List
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from supabase import create_client, Client  # pip install supabase
from TikTokApi import TikTokApi  # pip install TikTokApi (unofficial)

# Config
BOT_TOKEN = '8491490234:AAGWA_sw_2xzbB2m_z6dpA-iYlhNx2IAomQ'
SUPABASE_URL = 'https://uvoheubxepbyunumqmja.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV2b2hldWJ4ZXBieXVudW1xbWphIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE1OTU0MzMsImV4cCI6MjA3NzE3MTQzM30.GBmNWax6AJdze05IfQk6oWXF0mdaBjZDXzsUVBi8uQ4'
ADMIN_IDS = [5746625962]
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Supabase client (sync, wrap in to_thread for async)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Note: Before running, create/update tables in Supabase SQL Editor:
"""
-- Enable uuid-ossp extension if not already
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users table (restored tiktok_username for dynamic follows)
CREATE TABLE IF NOT EXISTS users (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,  -- Telegram username
    first_name TEXT,
    phone TEXT,
    tiktok_username TEXT UNIQUE,
    points INTEGER DEFAULT 0,
    referrals_count INTEGER DEFAULT 0,
    last_login TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_activity_seconds INTEGER DEFAULT 0,
    followed_current_picked BOOLEAN DEFAULT FALSE,  -- Track if followed current pick
    is_banned BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Bot settings for current picked TikTok (dynamic target)
CREATE TABLE IF NOT EXISTS bot_settings (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    value TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert initial setting if needed
INSERT INTO bot_settings (key, value) VALUES ('current_picked_tiktok', '') ON CONFLICT (key) DO NOTHING;

-- Points transactions
CREATE TABLE IF NOT EXISTS points_transactions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Referrals
CREATE TABLE IF NOT EXISTS referrals (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    referrer_id UUID REFERENCES users(id) ON DELETE CASCADE,
    referred_id UUID REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(referrer_id, referred_id)
);

-- Optional: Enable RLS for security
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;
-- Add policies as needed, e.g., for public leaderboard: CREATE POLICY "Public leaderboard" ON users FOR SELECT USING (is_banned = false);
"""

# Global: User sessions for activity tracking {user_id: {'last_msg_time': float, 'session_start': float}}
user_sessions: Dict[int, Dict] = {}

# Helper to run sync Supabase calls in async context
async def run_supabase(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

# TikTok verification (unofficial API) - now verifies against dynamic current picked
async def get_current_picked_tiktok() -> Optional[str]:
    """Get the current picked TikTok username from settings."""
    res = await run_supabase(
        lambda: supabase.table('bot_settings').select('value').eq('key', 'current_picked_tiktok').execute()
    )
    return res.data[0]['value'] if res.data else None

async def set_current_picked_tiktok(tiktok_username: str):
    """Set the current picked TikTok username in settings."""
    await run_supabase(
        lambda: supabase.table('bot_settings').upsert({'key': 'current_picked_tiktok', 'value': tiktok_username}).execute()
    )

async def verify_tiktok_follow(tiktok_username: str, target_tiktok: str) -> bool:
    """Verify if user follows the target TikTok account."""
    if not target_tiktok:
        return False
    try:
        with TikTokApi() as api:  # May need custom init with ms_tokens
            user = api.user(username=tiktok_username)
            follows = user.following_list(count=50)  # Paginate if needed; increase for more
            return any(f.username == target_tiktok for f in follows)
    except Exception as e:
        logger.error(f"TikTok verify error: {e}")
        return False  # Fallback; in prod, retry or manual

# DB Functions with Supabase (async-wrapped)
async def get_or_create_user(telegram_id: int, username: str = None, first_name: str = None) -> dict:
    """Get user from DB or create, with optional Telegram details."""
    res = await run_supabase(
        lambda: supabase.table('users').select('*').eq('telegram_id', telegram_id).execute()
    )
    if res.data:
        return res.data[0]
    # Create new
    data = {'telegram_id': telegram_id}
    if username:
        data['username'] = username
    if first_name:
        data['first_name'] = first_name
    await run_supabase(
        lambda: supabase.table('users').insert(data).execute()
    )
    return await get_or_create_user(telegram_id, username, first_name)  # Recurse to fetch

async def get_user_by_id(user_id: str) -> Optional[dict]:
    """Get user by internal ID."""
    res = await run_supabase(
        lambda: supabase.table('users').select('*').eq('id', user_id).execute()
    )
    return res.data[0] if res.data else None

async def get_all_users(page: int = 0, limit: int = 20) -> List[dict]:
    """Get all users with pagination."""
    res = await run_supabase(
        lambda: supabase.table('users').select('*').eq('is_banned', False).range(page * limit, (page + 1) * limit - 1).execute()
    )
    return res.data

async def get_user_count() -> int:
    """Get total active user count."""
    res = await run_supabase(
        lambda: supabase.table('users').select('count', count='exact').eq('is_banned', False).execute()
    )
    return res.count

async def get_transaction_count() -> int:
    """Get total transactions count."""
    res = await run_supabase(
        lambda: supabase.table('points_transactions').select('count', count='exact').execute()
    )
    return res.count

async def award_points(user_id: str, amount: int, ptype: str, desc: str):
    """Award points and log."""
    user = await get_user_by_id(user_id)
    if not user or user['is_banned']:
        return
    new_points = user['points'] + amount
    await run_supabase(
        lambda: supabase.table('users').update({'points': new_points}).eq('id', user_id).execute()
    )
    await run_supabase(
        lambda: supabase.table('points_transactions').insert({
            'user_id': user_id,
            'type': ptype,
            'amount': amount,
            'description': desc
        }).execute()
    )

async def check_daily_login(user: dict) -> bool:
    """Check if daily login awarded today."""
    last_login_str = user['last_login']
    last_login = datetime.fromisoformat(last_login_str.replace('Z', '+00:00')) if last_login_str else datetime.now()
    today = datetime.now().date()
    last_date = last_login.date()
    if today > last_date:
        await award_points(user['id'], 10, 'daily', 'Daily login')
        await run_supabase(
            lambda: supabase.table('users').update({'last_login': datetime.now()}).eq('id', user['id']).execute()
        )
        return True
    return False

async def update_activity(telegram_id: int, delta_seconds: int):
    """Update activity time; award points if threshold met."""
    if delta_seconds < 300:  # 5 min
        return
    points = delta_seconds // 300  # 1 per 5 min
    user = await get_or_create_user(telegram_id)
    if user['is_banned']:
        return
    await award_points(user['id'], points, 'activity', f'{delta_seconds}s activity')
    new_total = user['total_activity_seconds'] + delta_seconds
    await run_supabase(
        lambda: supabase.table('users').update({'total_activity_seconds': new_total}).eq('telegram_id', telegram_id).execute()
    )

def is_admin(telegram_id: int) -> bool:
    """Check if user is admin."""
    return telegram_id in ADMIN_IDS

# User Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    if await is_banned_user(user_id):
        await update.message.reply_text("❌ You are banned from this bot.")
        return
    user = await get_or_create_user(user_id, username, first_name)
    
    # Handle referral if in args
    if context.args and len(context.args) > 0 and context.args[0].startswith('ref_'):
        await handle_referral(update, context, user)
    
    if not user['phone'] or not user['tiktok_username']:
        # Request phone and TikTok with improved keyboard
        keyboard = [
            [KeyboardButton("📱 Share Phone", request_contact=True)],
            [InlineKeyboardButton("🎵 Enter TikTok @", callback_data=f"tiktok_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        current_picked = await get_current_picked_tiktok()
        picked_msg = f"\n\n💡 Current Pick to Follow: @{current_picked}" if current_picked else ""
        await update.message.reply_text(f"👋 Welcome! Complete registration:{picked_msg}\nShare your phone and TikTok username to start earning points!", reply_markup=reply_markup)
        return
    
    # Registered: Check daily, activity
    daily_awarded = await check_daily_login(user)
    daily_msg = " +10 points for daily login! 🎉" if daily_awarded else ""
    # Update session
    now = time.time()
    if user_id in user_sessions:
        delta = now - user_sessions[user_id]['last_msg_time']
        await update_activity(user_id, int(delta))
    user_sessions[user_id] = {'last_msg_time': now, 'session_start': now}
    
    # Show user menu
    keyboard = [
        [KeyboardButton("📊 My Profile"), KeyboardButton("🏆 Leaderboard")],
        [KeyboardButton("🎲 Pick Winner"), KeyboardButton("🍀 Lottery")],
        [KeyboardButton("🔗 Refer Friend"), KeyboardButton("📈 History")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    current_picked = await get_current_picked_tiktok()
    follow_status = "✅" if user['followed_current_picked'] else "❌"
    follow_msg = f"\n🎯 Follow Status: {follow_status} (Current: @{current_picked})" if current_picked else ""
    
    await update.message.reply_text(
        f"👋 Welcome back, {first_name or 'User'}!{daily_msg}{follow_msg}\n"
        f"💎 Your Points: {user['points']}\n"
        f"👥 Referrals: {user['referrals_count']}\n"
        f"⏱️ Total Activity: {user['total_activity_seconds'] // 60} min\n\n"
        f"🔗 Referral Link: t.me/{context.bot.username}?start=ref_{user_id}",
        reply_markup=reply_markup
    )

async def is_banned_user(telegram_id: int) -> bool:
    user = await get_or_create_user(telegram_id)
    return user.get('is_banned', False)

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE, referred_user: dict):
    if not context.args or not context.args[0].startswith('ref_'):
        return
    try:
        referrer_telegram_id = int(context.args[0].split('_')[1])
    except ValueError:
        return
    if await is_banned_user(referrer_telegram_id):
        return
    referrer = await get_or_create_user(referrer_telegram_id)
    # Check if already referred
    res = await run_supabase(
        lambda: supabase.table('referrals').select('*')
        .eq('referred_id', referred_user['id']).eq('referrer_id', referrer['id']).execute()
    )
    if res.data:
        return  # Already referred
    await run_supabase(
        lambda: supabase.table('referrals').insert({
            'referrer_id': referrer['id'],
            'referred_id': referred_user['id']
        }).execute()
    )
    await award_points(referrer['id'], 20, 'refer', f'Referred {referred_user["telegram_id"]}')
    new_refer_count = referrer['referrals_count'] + 1
    await run_supabase(
        lambda: supabase.table('users').update({'referrals_count': new_refer_count}).eq('id', referrer['id']).execute()
    )
    await update.message.reply_text("🎉 Referred successfully! Your friend has been rewarded, and so have you (+20 points)!")

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    contact = update.message.contact
    user = await get_or_create_user(telegram_id)
    if user['phone']:
        await update.message.reply_text("📱 Phone already registered.")
        return
    await run_supabase(
        lambda: supabase.table('users').update({'phone': contact.phone_number}).eq('telegram_id', telegram_id).execute()
    )
    await update.message.reply_text("✅ Phone saved! Now set your TikTok: /tiktok @username")

async def tiktok_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /tiktok @username\n\nExample: /tiktok @yourhandle")
        return
    username = context.args[0].lstrip('@')
    user = await get_or_create_user(telegram_id)
    if user['tiktok_username']:
        await update.message.reply_text("🎵 TikTok already set. Use /follow to verify current pick.")
        return
    await update.message.reply_text("🔍 Saving TikTok... Now verify follow with /follow")
    await run_supabase(
        lambda: supabase.table('users').update({'tiktok_username': username}).eq('telegram_id', telegram_id).execute()
    )

async def follow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify follow of current picked user."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    user = await get_or_create_user(telegram_id)
    if not user['tiktok_username']:
        await update.message.reply_text("❌ Set your TikTok first: /tiktok @username")
        return
    current_picked = await get_current_picked_tiktok()
    if not current_picked:
        await update.message.reply_text("❌ No current pick available. Wait for /pick or /lottery.")
        return
    if user['followed_current_picked']:
        await update.message.reply_text(f"✅ Already followed @{current_picked}! +50 points earned previously.")
        return
    await update.message.reply_text(f"🔍 Verifying follow of @{current_picked}... (This may take a moment)")
    verified = await verify_tiktok_follow(user['tiktok_username'], current_picked)
    if verified:
        await run_supabase(
            lambda: supabase.table('users').update({'followed_current_picked': True}).eq('telegram_id', telegram_id).execute()
        )
        await award_points(user['id'], 50, 'follow', f'Followed picked @{current_picked}')
        await update.message.reply_text(f"✅ Verified follow of @{current_picked}! +50 points earned! 🎉")
    else:
        await update.message.reply_text(f"❌ Not following @{current_picked} yet. Follow them and try /follow again.")

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user profile."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    user = await get_or_create_user(telegram_id)
    first_name = user.get('first_name', 'User')
    current_picked = await get_current_picked_tiktok()
    follow_status = "✅" if user['followed_current_picked'] else "❌"
    follow_msg = f"Follow Current Pick: {follow_status}" if current_picked else "No Current Pick"
    msg = f"""
👤 Profile: {first_name}
💎 Points: {user['points']}
👥 Referrals: {user['referrals_count']}
⏱️ Activity: {user['total_activity_seconds'] // 60} min
📱 Phone: {user['phone'] or 'Not set'}
🎵 TikTok: @{user['tiktok_username'] or 'Not set'}
{follow_msg}
"""
    await update.message.reply_text(msg)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent point history."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    user = await get_or_create_user(telegram_id)
    res = await run_supabase(
        lambda: supabase.table('points_transactions').select('*')
        .eq('user_id', user['id']).order('created_at', desc=True).limit(10).execute()
    )
    if not res.data:
        await update.message.reply_text("📈 No transaction history yet.")
        return
    msg = "📈 Recent History:\n"
    for tx in res.data:
        date = datetime.fromisoformat(tx['created_at'].replace('Z', '+00:00')).strftime('%m/%d %H:%M')
        msg += f"{date}: {tx['type'].title()} +{tx['amount']} ({tx['description'][:30]}...)\n"
    await update.message.reply_text(msg)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    res = await run_supabase(
        lambda: supabase.table('users').select('telegram_id, points, username')
        .eq('is_banned', False).order('points', desc=True).limit(10).execute()
    )
    if not res.data:
        await update.message.reply_text("🏆 No users on leaderboard yet.")
        return
    msg = "🏆 Top 10 Leaderboard:\n"
    for i, u in enumerate(res.data, 1):
        uname = f"@{u['username']}" if u['username'] else str(u['telegram_id'])
        msg += f"{i}. {uname}: {u['points']} pts\n"
    # Pagination button if more
    total = await get_user_count()
    if total > 10:
        keyboard = [[InlineKeyboardButton("Next Page ➡️", callback_data="leaderboard_next")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg += "\n(Showing top 10)"
        await update.message.reply_text(msg, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg)

async def pick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Weighted random pick based on points; sets current picked if they have TikTok."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    res = await run_supabase(
        lambda: supabase.table('users').select('id, points, telegram_id, username, tiktok_username')
        .eq('is_banned', False).gt('points', 0).eq('tiktok_username', None, is_not=True).execute()  # Prefer users with TikTok
    )
    if not res.data:
        res = await run_supabase(
            lambda: supabase.table('users').select('id, points, telegram_id, username, tiktok_username')
            .eq('is_banned', False).gt('points', 0).execute()
        )
    if not res.data:
        await update.message.reply_text("🎲 No users with points yet! Keep engaging!")
        return
    users = res.data
    total_points = sum(u['points'] for u in users)
    if total_points == 0:
        await update.message.reply_text("🎲 No points distributed yet!")
        return
    probs = [u['points'] / total_points for u in users]
    idx = random.choices(range(len(users)), weights=probs)[0]
    winner = users[idx]
    uname = f"@{winner['username']}" if winner['username'] else str(winner['telegram_id'])
    # Set as current picked if they have TikTok
    if winner['tiktok_username']:
        await set_current_picked_tiktok(winner['tiktok_username'])
        await update.message.reply_text(f"🎉 Winner Selected: {uname} ({winner['points']} pts)!\n\n🎯 New Pick: Follow @{winner['tiktok_username']} on TikTok for +50 pts (/follow)")
    else:
        await update.message.reply_text(f"🎉 Winner Selected: {uname} ({winner['points']} pts)!\nWeighted by points for fairness.")

async def lottery_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """7 top + 3 random; sets first top with TikTok as current picked."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    top_res = await run_supabase(
        lambda: supabase.table('users').select('telegram_id, points, username, tiktok_username')
        .eq('is_banned', False).order('points', desc=True).limit(7).execute()
    )
    top_users = []
    picked_set = False
    for u in top_res.data:
        uname = f"@{u['username']}" if u['username'] else str(u['telegram_id'])
        top_users.append(f"{uname} ({u['points']} pts)")
        if not picked_set and u['tiktok_username']:
            await set_current_picked_tiktok(u['tiktok_username'])
            picked_set = True
    
    all_res = await run_supabase(
        lambda: supabase.table('users').select('telegram_id, username').eq('is_banned', False).execute()
    )
    all_ids = [(u['telegram_id'], u['username']) for u in all_res.data if u['telegram_id'] not in [t['telegram_id'] for t in top_res.data]]
    random_users = random.sample(all_ids, min(3, len(all_ids))) if all_ids else []
    random_list = [f"@{un}" if un else str(tid) for tid, un in random_users]
    
    picked_msg = f"\n\n🎯 New Pick: Follow @{await get_current_picked_tiktok()} on TikTok for +50 pts (/follow)" if picked_set else ""
    msg = f"🎲 Lottery Winners:{picked_msg}\n\n"
    msg += "🥇 Top 7 (by points):\n" + "\n".join([f"- {name}" for name in top_users]) + "\n\n"
    msg += "🎰 Random 3 (new user boost):\n" + "\n".join([f"- {name}" for name in random_list])
    await update.message.reply_text(msg)

async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Referral info."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    user = await get_or_create_user(telegram_id)
    bot_username = context.bot.username
    link = f"t.me/{bot_username}?start=ref_{telegram_id}"
    await update.message.reply_text(
        f"👥 Invite friends & earn +20 pts each!\n\n"
        f"💎 Your Points: {user['points']}\n"
        f"👥 Referrals Made: {user['referrals_count']}\n\n"
        f"🔗 Share: {link}\n\n"
        f"💡 Tip: Send this in groups/chats!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages for activity and menu responses."""
    text = update.message.text
    user_id = update.effective_user.id
    if await is_banned_user(user_id):
        return
    now = time.time()
    if user_id in user_sessions:
        delta = now - user_sessions[user_id]['last_msg_time']
        if delta > 0:
            await update_activity(user_id, int(delta))
    user_sessions[user_id] = {'last_msg_time': now}
    
    # Simple menu responses for usability
    if text == "📊 My Profile":
        await profile_cmd(update, context)
    elif text == "🏆 Leaderboard":
        await leaderboard(update, context)
    elif text == "🎲 Pick Winner":
        await pick_cmd(update, context)
    elif text == "🍀 Lottery":
        await lottery_cmd(update, context)
    elif text == "🔗 Refer Friend":
        await refer_cmd(update, context)
    elif text == "📈 History":
        await history_cmd(update, context)
    else:
        # Optional: Fun response to engage
        await update.message.reply_text("💬 Thanks for chatting! Activity points ticking up... Use buttons or /help for commands.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help menu."""
    telegram_id = update.effective_user.id
    if await is_banned_user(telegram_id):
        return
    msg = """
🤖 Bot Commands:
/start - Welcome & daily reward
/profile - View your stats
/tiktok @username - Register TikTok
/follow - Verify follow of current pick (+50 pts!)
/leaderboard - Top users
/pick - Weighted random winner
/lottery - Top 7 + 3 random
/refer - Get your invite link
/history - Recent points

💡 Use buttons below for quick access!
"""
    await update.message.reply_text(msg)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()
    if query.data.startswith('tiktok_'):
        await query.edit_message_text("Please use /tiktok @username then /follow to verify.")
    elif query.data == "leaderboard_next":
        # Simple next page (extend as needed)
        await query.edit_message_text("🏆 Full leaderboard coming soon! /leaderboard for top 10.")

# Admin Handlers

async def admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if admin and reply if not."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Access denied. Admins only.")
        return False
    return True

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin menu with keyboard."""
    if not await admin_check(update, context):
        return
    keyboard = [
        [KeyboardButton("📢 Broadcast"), KeyboardButton("👥 Users")],
        [KeyboardButton("📊 Stats"), KeyboardButton("🔄 Reset Points")],
        [KeyboardButton("🚫 Ban User"), KeyboardButton("✅ Unban User")],
        [KeyboardButton("➕ Add Points")]  # New
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    msg = """
🔧 Admin Panel
Use buttons or commands below:
/broadcast <msg> - Announce to all
/users [page] - List users
/stats - System overview
/reset_all_points - Reset points (confirm)
/ban <id> - Ban user
/unban <id> - Unban
/add_points <id> <amount> - Manual points
/set_picked <tiktok> - Manually set current pick
"""
    await update.message.reply_text(msg, reply_markup=reply_markup)

async def set_picked_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin manually set current picked TikTok."""
    if not await admin_check(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_picked @tiktok_username")
        return
    tiktok = context.args[0].lstrip('@')
    await set_current_picked_tiktok(tiktok)
    # Reset followed flags for all users
    await run_supabase(
        lambda: supabase.table('users').update({'followed_current_picked': False}).execute()
    )
    await update.message.reply_text(f"✅ Set current pick to @{tiktok}. Follow flags reset.")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users."""
    if not await admin_check(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = ' '.join(context.args)
    users = await get_all_users()
    if not users:
        await update.message.reply_text("No users to broadcast to.")
        return
    await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")
    sent = 0
    failed = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user['telegram_id'], text=f"📢 Admin Announcement: {message}")
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send to {user['telegram_id']}: {e}")
            failed += 1
        await asyncio.sleep(0.05)  # Tighter rate limit
    await update.message.reply_text(f"✅ Sent: {sent} | ❌ Failed: {failed}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all users with pagination."""
    if not await admin_check(update, context):
        return
    page = int(context.args[0]) if context.args else 0
    users = await get_all_users(page)
    total = await get_user_count()
    if not users:
        await update.message.reply_text("No users.")
        return
    msg = f"👥 Active Users (Page {page+1}/{total//20 + 1}): {len(users)} shown\n\n"
    for i, u in enumerate(users, page*20 + 1):
        uname = f"@{u['username']}" if u['username'] else str(u['telegram_id'])
        status = "🚫 Banned" if u['is_banned'] else "✅ Active"
        msg += f"{i}. {uname} - {u['points']} pts {status}\n"
    # Pagination
    keyboard = []
    if page > 0:
        keyboard.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users_{page-1}"))
    if (page + 1) * 20 < total:
        keyboard.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_{page+1}"))
    if keyboard:
        reply_markup = InlineKeyboardMarkup([keyboard])
        await update.message.reply_text(msg, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """System stats."""
    if not await admin_check(update, context):
        return
    total_users = await get_user_count()
    res = await run_supabase(
        lambda: supabase.table('users').select('points', count='exact').eq('is_banned', False).execute()
    )
    total_pts = sum(u['points'] for u in res.data) if res.data else 0
    avg_pts = total_pts / total_users if total_users > 0 else 0
    tx_count = await get_transaction_count()
    current_picked = await get_current_picked_tiktok()
    followers_count = await run_supabase(
        lambda: supabase.table('users').select('count', count='exact').eq('followed_current_picked', True).eq('is_banned', False).execute()
    )
    msg = f"""
📊 System Stats:
- 👥 Active Users: {total_users}
- 💎 Total Points: {total_pts}
- 📈 Avg Points/User: {avg_pts:.2f}
- 🗂️ Transactions: {tx_count}
- 🎯 Current Pick: @{current_picked or 'None'}
- 👥 Followers of Pick: {followers_count.count}
"""
    await update.message.reply_text(msg)

async def add_points_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin add points manually."""
    if not await admin_check(update, context):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add_points <telegram_id> <amount>")
        return
    try:
        tg_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid ID or amount.")
        return
    user = await get_or_create_user(tg_id)
    if not user:
        await update.message.reply_text("User not found.")
        return
    await award_points(user['id'], amount, 'admin', f'Manual +{amount} by admin')
    uname = f"@{user['username']}" if user['username'] else str(tg_id)
    await update.message.reply_text(f"✅ Added {amount} points to {uname}. New total: {user['points'] + amount}")

async def reset_all_points_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset all points (confirm first)."""
    if not await admin_check(update, context):
        return
    keyboard = [[InlineKeyboardButton("⚠️ Confirm Reset All Points", callback_data="confirm_reset")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚠️ This will reset ALL active users' points to 0. Are you sure?", reply_markup=reply_markup)

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban user by telegram_id."""
    if not await admin_check(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <telegram_id>")
        return
    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid telegram_id.")
        return
    await run_supabase(
        lambda: supabase.table('users').update({'is_banned': True, 'points': 0}).eq('telegram_id', tg_id).execute()
    )
    await update.message.reply_text(f"🚫 User {tg_id} banned and points reset.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban user by telegram_id."""
    if not await admin_check(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <telegram_id>")
        return
    try:
        tg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid telegram_id.")
        return
    await run_supabase(
        lambda: supabase.table('users').update({'is_banned': False}).eq('telegram_id', tg_id).execute()
    )
    await update.message.reply_text(f"✅ User {tg_id} unbanned.")

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin callbacks."""
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_reset":
        await run_supabase(
            lambda: supabase.table('users').update({'points': 0}).eq('is_banned', False).execute()
        )
        await query.edit_message_text("🔄 All points reset for active users!")
        logger.info("Admin reset all points.")
    elif query.data.startswith("admin_users_"):
        page = int(query.data.split('_')[2])
        await users_cmd_callback(query, page)

async def users_cmd_callback(query, page: int):
    """Callback for admin users pagination."""
    users = await get_all_users(page)
    total = await get_user_count()
    if not users:
        await query.edit_message_text("No more users.")
        return
    msg = f"👥 Active Users (Page {page+1}/{total//20 + 1}): {len(users)} shown\n\n"
    for i, u in enumerate(users, page*20 + 1):
        uname = f"@{u['username']}" if u['username'] else str(u['telegram_id'])
        status = "🚫 Banned" if u['is_banned'] else "✅ Active"
        msg += f"{i}. {uname} - {u['points']} pts {status}\n"
    # Pagination
    keyboard = []
    if page > 0:
        keyboard.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users_{page-1}"))
    if (page + 1) * 20 < total:
        keyboard.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_{page+1}"))
    reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
    await query.edit_message_text(msg, reply_markup=reply_markup)

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin menu button presses."""
    text = update.message.text
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if text == "📢 Broadcast":
        await update.message.reply_text("Usage: /broadcast <your message>")
    elif text == "👥 Users":
        await users_cmd(update, context)
    elif text == "📊 Stats":
        await stats_cmd(update, context)
    elif text == "🔄 Reset Points":
        await reset_all_points_cmd(update, context)
    elif text == "🚫 Ban User":
        await update.message.reply_text("Usage: /ban <telegram_id>")
    elif text == "✅ Unban User":
        await update.message.reply_text("Usage: /unban <telegram_id>")
    elif text == "➕ Add Points":
        await update.message.reply_text("Usage: /add_points <telegram_id> <amount>")

def main():
    # Tables must be created in Supabase dashboard/SQL editor (see note above)
    if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Missing required env vars: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # User Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("tiktok", tiktok_cmd))
    app.add_handler(CommandHandler("follow", follow_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("pick", pick_cmd))
    app.add_handler(CommandHandler("lottery", lottery_cmd))
    app.add_handler(CommandHandler("refer", refer_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Admin Handlers
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("set_picked", set_picked_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("add_points", add_points_cmd))
    app.add_handler(CommandHandler("reset_all_points", reset_all_points_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_message), group=1)  # Admin group after user
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^(confirm_reset|admin_users_)"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🤖 Advanced Bot with Dynamic TikTok Pick starting...")
    app.run_polling()

if __name__ == '__main__':
    main()