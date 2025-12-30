import asyncio
import re
import datetime
import sys
import os
import threading
import logging

from datetime import datetime, timedelta
from telethon import TelegramClient, events, Button
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask

# === KONFIGURASI TELEGRAM ===
api_id = int(os.getenv('TG_API_ID', '27165484'))
api_hash = os.getenv('TG_API_HASH', 'b5f28de58166f16d6fedc4e0fd29a859')
client = TelegramClient('user_session', api_id, api_hash)

# === SETUP LOGGER ===
logging.basicConfig(
    filename='bot.log',
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s'
    )

# === SCHEDULER ===
scheduler = AsyncIOScheduler()

# === DATA GLOBAL ===
blacklisted_groups = set()
job_data = {}
delay_setting = {}
MASA_AKTIF = datetime(2030, 12, 31)
delay_per_group_setting = {}  # Menyimpan delay per user (detik)
pesan_simpan = {}  # key: user_id, value: pesan terbaru
preset_pesan = {}  # key: user_id, value: {nama_preset: isi_pesan}
usage_stats = {}  # key: user_id, value: jumlah pesan yang berhasil dikirim
start_time = datetime.now()
TOTAL_SENT_MESSAGES = 0
JOBS = {}
active_forward_tasks = {}  # key: user_id, value: {'stop_flag': bool, 'task': asyncio.Task}
DEFAULT_ALLOWED_USERS = {7605681637, 1538087933, 1735348722}  # Ganti dengan ID admin awal
ALLOWED_USERS_FILE = 'allowed_users.txt'

# === FUNGSI LOAD & SAVE ALLOWED USERS ===
def load_allowed_users():
    users = set()
    try:
        with open(ALLOWED_USERS_FILE, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    users.add(int(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        users = set(DEFAULT_ALLOWED_USERS)
        try:
            with open(ALLOWED_USERS_FILE, 'w', encoding='utf-8') as f:
                f.write("\n".join(map(str, sorted(users))))
        except Exception:
            pass
    if not users:
        users = set(DEFAULT_ALLOWED_USERS)
    return users

def save_allowed_users(users=None):
    if users is None:
        users = ALLOWED_USERS
    with open(ALLOWED_USERS_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(map(str, sorted(set(users)))))

ALLOWED_USERS = load_allowed_users()

async def is_allowed(event):
    try:
        sender = await event.get_sender()
        return bool(sender) and sender.id in ALLOWED_USERS
    except Exception as e:
        logging.error(f"[ERROR] Saat memeriksa is_allowed: {e}")
        return False

async def require_allowed(event):
    if await is_allowed(event):
        return True
    try:
        await event.respond("❌ Kamu tidak punya izin untuk menggunakan perintah ini.")
    except Exception:
        pass
    return False

HARI_MAPPING = {
    'senin': 'monday', 'selasa': 'tuesday', 'rabu': 'wednesday',
    'kamis': 'thursday', 'jumat': 'friday', 'sabtu': 'saturday',
    'minggu': 'sunday'
}

def update_usage(user_id, count):
    global TOTAL_SENT_MESSAGES
    usage_stats[user_id] = usage_stats.get(user_id, 0) + count
    TOTAL_SENT_MESSAGES += count

# === FUNGSI UNTUK MELAKUKAN FORWARDING PESAN ===
async def forward_job(
    user_id, mode, source, message_id_or_text,
    jumlah_grup, durasi_menit: float, jumlah_pesan,
    delay_per_group: int = 0
):
    start = datetime.now()
    end = start + timedelta(minutes=durasi_menit)
    jeda_batch = delay_setting.get(user_id, 5)
    if delay_per_group <= 0:
        delay_per_group = delay_per_group_setting.get(user_id, 0)
    now = datetime.now()
    next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    harian_counter = 0
    total_counter = 0
    
    # Inisialisasi stop flag untuk user ini
    if user_id not in active_forward_tasks:
        active_forward_tasks[user_id] = {'stop_flag': False}
    active_forward_tasks[user_id]['stop_flag'] = False
    
    # Parse multiple messages jika ada (dipisahkan oleh ||)
    if isinstance(message_id_or_text, str) and '||' in message_id_or_text:
        messages_list = [msg.strip() for msg in message_id_or_text.split('||') if msg.strip()]
    else:
        messages_list = [message_id_or_text]

    durasi_jam = durasi_menit / 60.0
    info_msg = f"[{now:%H:%M:%S}] Mulai meneruskan pesan selama {durasi_menit:.2f} menit ({durasi_jam:.2f} jam)~"
    print(info_msg)
    logging.info(info_msg)

    try:
        pesan_info = f"Sedang meneruskan pesan...\nDurasi: {durasi_menit:.2f} menit\nTarget harian: {jumlah_pesan} pesan."
        if len(messages_list) > 1:
            pesan_info += f"\nTotal pesan berbeda: {len(messages_list)}"
        await client.send_message(user_id, pesan_info)
    except Exception as e:
        logging.error(f"Error kirim notifikasi ke {user_id}: {e}")

    while datetime.now() < end:
        # Cek stop flag
        if active_forward_tasks.get(user_id, {}).get('stop_flag', False):
            stop_msg = f"[{datetime.now():%H:%M:%S}] Pengiriman dihentikan oleh user."
            print(stop_msg)
            logging.info(stop_msg)
            try:
                await client.send_message(
                    user_id,
                    f"Pengiriman pesan dihentikan.\nTotal terkirim: {total_counter} pesan."
                )
            except Exception as e:
                logging.error(f"Error kirim notifikasi stop ke {user_id}: {e}")
            break
            
        if datetime.now() >= next_reset:
            harian_counter = 0
            next_reset += timedelta(days=1)
            reset_msg = (
                f"[{datetime.now():%H:%M:%S}] Reset harian: lanjut besok yaa, sayang!"
            )
            print(reset_msg)
            logging.info(reset_msg)

        counter = 0
        total_dialogs_checked = 0
        groups_found = 0
        groups_skipped = 0
        
        logging.info(f"Mulai iterasi dialog untuk {jumlah_grup} grup, durasi {durasi_menit} menit")
        
        async for dialog in client.iter_dialogs():
            total_dialogs_checked += 1
            # Cek stop flag lagi di dalam loop
            if active_forward_tasks.get(user_id, {}).get('stop_flag', False):
                logging.info(f"Stop flag aktif. Total dialog dicek: {total_dialogs_checked}, Grup ditemukan: {groups_found}, Grup di-skip: {groups_skipped}")
                break
                
            if datetime.now() >= end:
                logging.info(f"Waktu habis. Total dialog dicek: {total_dialogs_checked}, Grup ditemukan: {groups_found}, Grup di-skip: {groups_skipped}")
                break
            if harian_counter >= jumlah_pesan:
                logging.info(f"Target harian tercapai. Total dialog dicek: {total_dialogs_checked}, Grup ditemukan: {groups_found}, Grup di-skip: {groups_skipped}")
                break
            
            # Cek apakah ini grup atau supergroup (bukan channel atau private chat)
            is_group_type = False
            try:
                # Cek berbagai tipe grup
                if dialog.is_group:
                    is_group_type = True
                elif hasattr(dialog.entity, 'megagroup') and dialog.entity.megagroup:
                    is_group_type = True
                elif hasattr(dialog.entity, '__class__'):
                    class_name = dialog.entity.__class__.__name__
                    if 'Chat' in class_name or 'Channel' in class_name:
                        # Cek apakah ini bukan broadcast channel
                        if hasattr(dialog.entity, 'broadcast') and not dialog.entity.broadcast:
                            is_group_type = True
                        elif not hasattr(dialog.entity, 'broadcast'):
                            # Jika tidak ada atribut broadcast, anggap sebagai grup
                            is_group_type = True
            except Exception as e:
                logging.error(f"Error cek tipe dialog: {e}")
                continue
            
            if not is_group_type:
                continue
            
            # Cek blacklist
            dialog_name = dialog.name or "Tanpa Nama"
            if dialog_name in blacklisted_groups:
                groups_skipped += 1
                logging.debug(f"Grup {dialog_name} di-skip karena ada di blacklist")
                continue
            
            groups_found += 1
            logging.debug(f"Grup ditemukan: {dialog_name} (Total ditemukan: {groups_found})")
            try:
                # Kirim SEMUA pesan dari messages_list ke grup ini sebagai bubble chat terpisah
                pesan_terkirim_grup = 0
                for idx, current_message in enumerate(messages_list):
                    # Cek stop flag sebelum setiap pesan
                    if active_forward_tasks.get(user_id, {}).get('stop_flag', False):
                        break
                    if datetime.now() >= end or harian_counter >= jumlah_pesan:
                        break
                    
                    if mode == 'forward':
                        # Untuk forward mode dengan multiple messages
                        # current_message bisa berupa ID saja atau "source id"
                        msg_id = None
                        src = source
                        
                        if isinstance(current_message, int):
                            msg_id = current_message
                        elif isinstance(current_message, str):
                            # Cek apakah hanya angka (ID saja)
                            if current_message.strip().isdigit():
                                msg_id = int(current_message.strip())
                            else:
                                # Mungkin format "source id" atau hanya ID dengan spasi
                                parts = current_message.strip().split(maxsplit=1)
                                if len(parts) == 2:
                                    src = parts[0]
                                    try:
                                        msg_id = int(parts[1])
                                    except ValueError:
                                        msg_id = int(current_message.strip())
                                else:
                                    # Coba parse sebagai integer
                                    try:
                                        msg_id = int(current_message.strip())
                                    except ValueError:
                                        # Fallback ke message_id_or_text original jika list kosong
                                        if isinstance(message_id_or_text, int):
                                            msg_id = message_id_or_text
                                        elif isinstance(message_id_or_text, str) and message_id_or_text.isdigit():
                                            msg_id = int(message_id_or_text)
                                        else:
                                            continue
                        
                        if msg_id is not None:
                            try:
                                msg = await client.get_messages(src, ids=msg_id)
                                if msg:
                                    await client.forward_messages(
                                        dialog.id, msg.id, from_peer=src
                                    )
                                    pesan_terkirim_grup += 1
                                    harian_counter += 1
                                    total_counter += 1
                                    update_usage(user_id, 1)
                                    
                                    # Delay kecil antar pesan dalam grup yang sama
                                    if idx < len(messages_list) - 1 and delay_per_group > 0:
                                        await asyncio.sleep(min(delay_per_group, 2))  # Max 2 detik antar pesan
                            except Exception as e:
                                error_msg = (
                                    f"[{datetime.now():%H:%M:%S}] Gagal forward pesan {idx+1}/{len(messages_list)} ke grup {dialog.name}: {e}"
                                )
                                print(error_msg)
                                logging.error(error_msg)
                                continue
                    else:
                        # Text mode - kirim pesan dari list
                        try:
                            await client.send_message(
                                dialog.id, current_message, link_preview=True
                            )
                            pesan_terkirim_grup += 1
                            harian_counter += 1
                            total_counter += 1
                            update_usage(user_id, 1)
                            
                            # Delay kecil antar pesan dalam grup yang sama
                            if idx < len(messages_list) - 1 and delay_per_group > 0:
                                await asyncio.sleep(min(delay_per_group, 2))  # Max 2 detik antar pesan
                        except Exception as e:
                            error_msg = (
                                f"[{datetime.now():%H:%M:%S}] Gagal kirim pesan {idx+1}/{len(messages_list)} ke grup {dialog.name}: {e}"
                            )
                            print(error_msg)
                            logging.error(error_msg)
                            continue
                
                if pesan_terkirim_grup > 0:
                    counter += 1
                    log_msg = (
                        f"[{datetime.now():%H:%M:%S}] Sukses kirim {pesan_terkirim_grup} pesan ke grup: {dialog.name}"
                    )
                    if len(messages_list) > 1:
                        log_msg += f" ({len(messages_list)} bubble chat berbeda)"
                    print(log_msg)
                    logging.info(log_msg)

                if delay_per_group > 0:
                    await asyncio.sleep(delay_per_group)

                if counter >= jumlah_grup or harian_counter >= jumlah_pesan:
                    break

            except Exception as e:
                error_msg = (
                    f"[{datetime.now():%H:%M:%S}] Gagal dikirim ke grup {dialog.name}: {e}"
                )
                print(error_msg)
                logging.error(error_msg)
                continue

        # Cek stop flag setelah batch
        if active_forward_tasks.get(user_id, {}).get('stop_flag', False):
            break

        # Log statistik setelah batch
        logging.info(f"Batch selesai: {counter} grup, {total_counter} pesan terkirim. Total dialog dicek: {total_dialogs_checked}, Grup ditemukan: {groups_found}, Grup di-skip: {groups_skipped}")

        if harian_counter >= jumlah_pesan:
            notif = (
                f"Target harian {jumlah_pesan} pesan tercapai!\n"
                "Bot lanjut lagi besok bub, tetap semangat sebarnya ya!"
            )
            info_notif = f"[{datetime.now():%H:%M:%S}] {notif}"
            print(info_notif)
            logging.info(info_notif)
            try:
                await client.send_message(user_id, notif)
            except Exception as e:
                logging.error(f"Error kirim notifikasi ke {user_id}: {e}")

            sleep_seconds = (next_reset - datetime.now()).total_seconds()
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        else:
            batch_msg = (
                f"[{datetime.now():%H:%M:%S}] Batch {counter} grup selesai. Istirahat {jeda_batch} detik dulu ya..."
            )
            print(batch_msg)
            logging.info(batch_msg)
            await asyncio.sleep(jeda_batch)

    # Hapus stop flag setelah selesai
    if user_id in active_forward_tasks:
        active_forward_tasks[user_id]['stop_flag'] = False

    durasi_jam = durasi_menit / 60.0
    selesai = (
        f"Forward selesai!\nTotal terkirim ke {total_counter} grup selama {durasi_menit:.2f} menit ({durasi_jam:.2f} jam)."
    )
    selesai_msg = f"[{datetime.now():%H:%M:%S}] {selesai}"
    print(selesai_msg)
    logging.info(selesai_msg)
    try:
        await client.send_message(user_id, selesai)
    except Exception as e:
        logging.error(f"Error kirim pesan selesai ke {user_id}: {e}")

# === PERINTAH BOT ===

@client.on(events.NewMessage(pattern='/scheduleforward'))
async def schedule_cmd(event):
    if not await require_allowed(event):
        return
    raw = event.message.raw_text.strip()
    parts = raw.split(maxsplit=2)
    if len(parts) < 3:
        return await event.respond(
            "Format salah:\n"
            "/scheduleforward mode pesan/sumber jumlah_grup durasi_menit jeda jumlah_pesan hari1,hari2 jam:menit"
        )
    try:
        mode = parts[1].lower()
        rest = parts[2].strip()
        sisa = rest.rsplit(' ', 6)
        if len(sisa) != 7:
            return await event.respond("Format tidak sesuai. Pastikan argumen lengkap!")

        pesan_atau_sumber, jumlah, durasi, jeda, jumlah_pesan, hari_str, waktu = sisa
        jumlah = int(jumlah)
        durasi = float(durasi)  # Sekarang dalam menit
        jeda = int(jeda)
        jumlah_pesan = int(jumlah_pesan)
        jam, menit = map(int, waktu.split(':'))
        hari_list = [HARI_MAPPING.get(h.lower()) for h in hari_str.split(',')]
        if None in hari_list:
            return await event.respond(
                "Terdapat nama hari yang tidak valid. Gunakan: senin,selasa,...,minggu."
            )

        source = ''
        message_id_or_text = pesan_atau_sumber
        if mode == 'forward':
            fp = pesan_atau_sumber.split(maxsplit=1)
            if len(fp) != 2:
                return await event.respond(
                    "Format forward schedule harus menyertakan sumber dan ID pesan.\n"
                    "Contoh: /scheduleforward forward @channel 123 20 120 5 300 senin,jumat 08:00"
                )
            source = fp[0]
            message_id_or_text = fp[1]  # Bisa jadi string jika multiple messages

        for hari_eng in hari_list:
            job_id = f"{event.sender_id}{hari_eng}{int(datetime.now().timestamp())}"
            job_data[job_id] = {
                'user': event.sender_id,
                'mode': mode,
                'source': source,
                'message': pesan_atau_sumber,
                'jumlah': jumlah,
                'durasi': durasi,
                'jeda': jeda,
                'jumlah_pesan': jumlah_pesan
            }
            delay_setting[event.sender_id] = jeda
            job = scheduler.add_job(
                forward_job,
                trigger=CronTrigger(day_of_week=hari_eng, hour=jam, minute=menit),
                args=[event.sender_id, mode, source, message_id_or_text, jumlah, durasi, jumlah_pesan],
                id=job_id
            )
            JOBS[job_id] = job

        daftar_hari = ', '.join([h.title() for h in hari_str.split(',')])
        await event.respond(
            f"Jadwal forward berhasil ditambahkan untuk hari {daftar_hari} pukul {waktu}!"
        )
    except Exception as e:
        err_msg = f"Error: {e}"
        logging.error(err_msg)
        await event.respond(err_msg)

@client.on(events.NewMessage(pattern='/forward'))
async def forward_sekarang(event):
    if not await require_allowed(event):
        return
    raw = event.message.raw_text.strip()
    parts = raw.split(maxsplit=2)
    if len(parts) < 3:
        return await event.respond(
            "Format salah:\n"
            "/forward mode sumber/id/isipesan jumlah_grup jeda durasi_menit jumlah_pesan\n\n"
            "Untuk multiple pesan, pisahkan dengan ||\n"
            "Contoh: /forward text \"Pesan 1\"||\"Pesan 2\"||\"Pesan 3\" 10 5 60 300"
        )
    try:
        mode = parts[1].lower()
        rest = parts[2].strip()
        
        if mode == 'forward':
            # Parse forward mode
            # Cari posisi parameter numerik terakhir (jumlah_grup, jeda, durasi, jumlah_pesan)
            # Format: source id1||id2||id3 jumlah_grup jeda durasi jumlah_pesan
            fparts = rest.split()
            if len(fparts) < 6:
                return await event.respond(
                    "Format salah:\n"
                    "/forward forward <sumber> <id_pesan> [||<id_pesan2>]... <jumlah_grup> <jeda> <durasi_menit> <jumlah_pesan>"
                )
            
            # Parameter selalu di akhir: jumlah_grup jeda durasi jumlah_pesan
            jumlah = int(fparts[-4])
            jeda_batch = int(fparts[-3])
            durasi = float(fparts[-2])
            jumlah_pesan = int(fparts[-1])
            
            # Source adalah bagian pertama
            source = fparts[0]
            
            # Message IDs adalah bagian antara source dan parameter
            # Bisa single ID atau multiple IDs dipisahkan ||
            message_parts = ' '.join(fparts[1:-4])
            message_str = message_parts  # Bisa berisi || untuk multiple messages
            
            delay_setting[event.sender_id] = jeda_batch
            # Simpan task info untuk bisa di-stop
            if event.sender_id not in active_forward_tasks:
                active_forward_tasks[event.sender_id] = {'stop_flag': False}
            task = asyncio.create_task(
                forward_job(event.sender_id, mode, source, message_str, jumlah, durasi, jumlah_pesan)
            )
            active_forward_tasks[event.sender_id]['task'] = task
            await task
            
        elif mode == 'text':
            # Parse text mode
            # Format: "pesan1"||"pesan2"||... jumlah_grup jeda durasi jumlah_pesan
            # Atau: pesan1||pesan2||... jumlah_grup jeda durasi jumlah_pesan
            
            # Split dari kanan untuk mendapatkan parameter numerik
            sisa = rest.rsplit(' ', 4)
            if len(sisa) < 4:
                return await event.respond(
                    "Format salah:\n"
                    "/forward text <pesan1>||<pesan2>||... <jumlah_grup> <jeda> <durasi_menit> <jumlah_pesan>"
                )
            
            text_part = sisa[0]  # Bisa berisi || untuk multiple messages
            jumlah = int(sisa[1])
            jeda_batch = int(sisa[2])
            durasi = float(sisa[3])
            jumlah_pesan = int(sisa[4]) if len(sisa) >= 5 else 300
            
            # Bersihkan quotes jika ada
            text = text_part.strip().strip('"').strip("'")
            
            delay_setting[event.sender_id] = jeda_batch
            pesan_simpan[event.sender_id] = text
            # Simpan task info untuk bisa di-stop
            if event.sender_id not in active_forward_tasks:
                active_forward_tasks[event.sender_id] = {'stop_flag': False}
            task = asyncio.create_task(
                forward_job(event.sender_id, mode, '', text, jumlah, durasi, jumlah_pesan)
            )
            active_forward_tasks[event.sender_id]['task'] = task
            await task
        else:
            await event.respond("Mode harus 'forward' atau 'text'")
    except Exception as e:
        err_msg = f"Error: {e}"
        logging.error(err_msg)
        await event.respond(err_msg)

@client.on(events.NewMessage(pattern='/setdelay'))
async def set_delay(event):
    if not await require_allowed(event):
        return
    try:
        parts = event.message.raw_text.split(maxsplit=1)
        delay = int(parts[1])
        delay_setting[event.sender_id] = delay
        await event.respond(f"Jeda antar batch diset ke {delay} detik!")
    except Exception as e:
        logging.error(f"Error pada /setdelay: {e}")
        await event.respond("Gunakan: /setdelay <detik>")

@client.on(events.NewMessage(pattern=r'/setdelaygroup\s+(\d+)'))
async def set_delay_group(event):
    if not await require_allowed(event):
        return
    try:
        delay = int(event.pattern_match.group(1))
        delay_per_group_setting[event.sender_id] = delay
        await event.respond(f"Delay antar grup diset ke {delay} detik!")
    except Exception as e:
        logging.error(f"Error pada /setdelaygroup: {e}")
        await event.respond("Gunakan: /setdelaygroup <detik>")

@client.on(events.NewMessage(pattern='/cekdelaygroup'))
async def cek_delay_group(event):
    if not await require_allowed(event):
        return
    delay = delay_per_group_setting.get(event.sender_id, 0)
    await event.respond(f"Delay antar grup saat ini: {delay} detik")

@client.on(events.NewMessage(pattern='/resetdelaygroup'))
async def reset_delay_group(event):
    if not await require_allowed(event):
        return
    delay_per_group_setting.pop(event.sender_id, None)
    await event.respond("Delay antar grup telah direset ke default (0 detik)")

@client.on(events.NewMessage(pattern='/review_pesan'))
async def review_pesan(event):
    if not await require_allowed(event):
        return
    user_id = event.sender_id
    pesan = pesan_simpan.get(user_id, "Belum ada pesan default yang disimpan.")
    
    buttons = [
        [Button.inline("Ubah Pesan", data=b"ubah_pesan_menu")]
    ]
    await event.respond(f"Pesan default Anda:\n\n{pesan}", buttons=buttons)

@client.on(events.NewMessage(pattern=r'/ubah_pesan(?:\s+(.+))?$'))
async def ubah_pesan(event):
    if not await require_allowed(event):
        return
    try:
        raw_text = event.message.raw_text.strip()
        parts = raw_text.split(maxsplit=1)
        if len(parts) < 2:
            return await event.respond("Format salah. Gunakan: /ubah_pesan <pesan baru>")
        pesan_baru = parts[1]
        pesan_simpan[event.sender_id] = pesan_baru.strip()
        await event.respond(f"Pesan default berhasil diubah menjadi:\n\n{pesan_baru.strip()}")
    except Exception as e:
        logging.error(f"Error pada /ubah_pesan: {e}")
        await event.respond("Format salah. Gunakan: /ubah_pesan <pesan baru>")

@client.on(events.NewMessage(pattern=r'/simpan_preset'))
async def simpan_preset(event):
    if not await require_allowed(event):
        return
    try:
        raw_text = event.message.raw_text.strip()
        parts = raw_text.split(maxsplit=2)
        if len(parts) < 3:
            return await event.respond("Format salah. Gunakan: /simpan_preset <nama_preset> <isi_pesan>")
        
        nama_preset = parts[1]
        isi_pesan = parts[2]
        
        user_id = event.sender_id
        if user_id not in preset_pesan:
            preset_pesan[user_id] = {}
        
        preset_pesan[user_id][nama_preset] = isi_pesan.strip()
        await event.respond(f"Preset '{nama_preset}' berhasil disimpan!")
    except Exception as e:
        logging.error(f"Error pada /simpan_preset: {e}")
        await event.respond("Format salah. Gunakan: /simpan_preset <nama_preset> <isi_pesan>")

@client.on(events.NewMessage(pattern=r'/pakai_preset\s+(\S+)'))
async def pakai_preset(event):
    if not await require_allowed(event):
        return
    try:
        nama_preset = event.pattern_match.group(1)
        user_id = event.sender_id
        
        if user_id not in preset_pesan or nama_preset not in preset_pesan[user_id]:
            return await event.respond(f"Preset '{nama_preset}' tidak ditemukan!")
        
        pesan_simpan[user_id] = preset_pesan[user_id][nama_preset]
        await event.respond(f"Preset '{nama_preset}' telah dipilih sebagai pesan default:\n\n{preset_pesan[user_id][nama_preset]}")
    except Exception as e:
        logging.error(f"Error pada /pakai_preset: {e}")
        await event.respond("Format salah. Gunakan: /pakai_preset <nama_preset>")

@client.on(events.NewMessage(pattern='/list_preset'))
async def list_preset(event):
    if not await require_allowed(event):
        return
    user_id = event.sender_id
    
    if user_id not in preset_pesan or not preset_pesan[user_id]:
        return await event.respond("Anda belum memiliki preset pesan.")
    
    teks = "Daftar preset pesan Anda:\n\n"
    buttons = []
    for idx, (nama, isi) in enumerate(preset_pesan[user_id].items(), 1):
        preview = isi[:50] + "..." if len(isi) > 50 else isi
        teks += f"{idx}. {nama}\n   Preview: {preview}\n\n"
        # Button untuk pakai dan hapus preset
        buttons.append([
            Button.inline(f"Pakai {nama}", data=f"pakai_{user_id}_{nama}".encode()),
            Button.inline(f"Hapus {nama}", data=f"hapus_{user_id}_{nama}".encode())
        ])
    
    await event.respond(teks, buttons=buttons)

@client.on(events.NewMessage(pattern=r'/edit_preset'))
async def edit_preset(event):
    if not await require_allowed(event):
        return
    try:
        raw_text = event.message.raw_text.strip()
        parts = raw_text.split(maxsplit=2)
        if len(parts) < 3:
            return await event.respond("Format salah. Gunakan: /edit_preset <nama_preset> <isi_baru>")
        
        nama_preset = parts[1]
        isi_baru = parts[2]
        
        user_id = event.sender_id
        if user_id not in preset_pesan or nama_preset not in preset_pesan[user_id]:
            return await event.respond(f"Preset '{nama_preset}' tidak ditemukan!")
        
        preset_pesan[user_id][nama_preset] = isi_baru.strip()
        await event.respond(f"Preset '{nama_preset}' berhasil diubah!")
    except Exception as e:
        logging.error(f"Error pada /edit_preset: {e}")
        await event.respond("Format salah. Gunakan: /edit_preset <nama_preset> <isi_baru>")

@client.on(events.NewMessage(pattern=r'/hapus_preset\s+(\S+)'))
async def hapus_preset(event):
    if not await require_allowed(event):
        return
    try:
        nama_preset = event.pattern_match.group(1)
        user_id = event.sender_id
        
        if user_id not in preset_pesan or nama_preset not in preset_pesan[user_id]:
            return await event.respond(f"Preset '{nama_preset}' tidak ditemukan!")
        
        del preset_pesan[user_id][nama_preset]
        await event.respond(f"Preset '{nama_preset}' berhasil dihapus!")
    except Exception as e:
        logging.error(f"Error pada /hapus_preset: {e}")
        await event.respond("Format salah. Gunakan: /hapus_preset <nama_preset>")

@client.on(events.NewMessage(pattern='/review'))
async def review_jobs(event):
    if not await require_allowed(event):
        return
    user_id = event.sender_id
    teks = "== Jadwal Aktif ==\n"
    buttons = []
    if not job_data:
        teks += "Tidak ada jadwal."
    else:
        user_jobs = {jid: info for jid, info in job_data.items() if info.get('user') == user_id}
        if not user_jobs:
            teks += "Tidak ada jadwal untuk Anda."
        else:
            for job_id, info in user_jobs.items():
                durasi_jam = info['durasi'] / 60.0
                teks += (
                    f"- ID: {job_id[:20]}...\n"
                    f"  Mode: {info['mode']}\n"
                    f"  Grup: {info['jumlah']}\n"
                    f"  Durasi: {info['durasi']} menit ({durasi_jam:.2f} jam)\n\n"
                )
                buttons.append([Button.inline(f"Hapus {job_id[:15]}...", data=f"delete_{job_id}".encode())])
    await event.respond(teks, buttons=buttons if buttons else None)

@client.on(events.NewMessage(pattern='/deletejob'))
async def delete_job(event):
    if not await require_allowed(event):
        return
    try:
        job_id = event.message.raw_text.split()[1]
        scheduler.remove_job(job_id)
        job_data.pop(job_id, None)
        JOBS.pop(job_id, None)
        await event.respond("Jadwal berhasil dihapus!")
    except Exception as e:
        logging.error(f"Error pada /deletejob: {e}")
        await event.respond("Gagal menghapus. Pastikan ID yang dimasukkan benar.")

@client.on(events.NewMessage(pattern='/stopforward'))
async def stop_forward(event):
    if not await require_allowed(event):
        return
    user_id = event.sender_id
    removed = []
    
    # Hentikan scheduled jobs
    for job in scheduler.get_jobs():
        if str(user_id) in job.id:
            try:
                scheduler.remove_job(job.id)
                job_data.pop(job.id, None)
                JOBS.pop(job.id, None)
                removed.append(job.id)
            except Exception as e:
                logging.error(f"Error menghapus job {job.id}: {e}")
    
    # Hentikan active forward tasks
    stopped_active = False
    if user_id in active_forward_tasks:
        active_forward_tasks[user_id]['stop_flag'] = True
        stopped_active = True
        logging.info(f"Stop flag diaktifkan untuk user {user_id}")
    
    if removed or stopped_active:
        msg = []
        if removed:
            msg.append(f"Job terjadwal dihapus: {', '.join(removed)}")
        if stopped_active:
            msg.append("Pengiriman pesan aktif sedang dihentikan...")
        await event.respond("\n".join(msg))
    else:
        await event.respond("Tidak ditemukan job forward aktif untuk Anda.")

@client.on(events.NewMessage(pattern='/blacklist_add'))
async def add_blacklist(event):
    if not await require_allowed(event):
        return
    try:
        nama = " ".join(event.message.raw_text.split()[1:])
        blacklisted_groups.add(nama)
        await event.respond(f"'{nama}' berhasil masuk ke blacklist!")
    except Exception as e:
        logging.error(f"Error pada /blacklist_add: {e}")
        await event.respond("Format salah. Gunakan: /blacklist_add <nama>")

@client.on(events.NewMessage(pattern='/blacklist_remove'))
async def remove_blacklist(event):
    if not await require_allowed(event):
        return
    try:
        nama = " ".join(event.message.raw_text.split()[1:])
        blacklisted_groups.discard(nama)
        await event.respond(f"'{nama}' telah dihapus dari blacklist!")
    except Exception as e:
        logging.error(f"Error pada /blacklist_remove: {e}")
        await event.respond("Format salah. Gunakan: /blacklist_remove <nama>")

@client.on(events.NewMessage(pattern='/list_blacklist'))
async def list_blacklist(event):
    if not await require_allowed(event):
        return
    if not blacklisted_groups:
        await event.respond("Blacklist kosong!")
    else:
        teks = "== Grup dalam blacklist ==\n\n"
        buttons = []
        blacklist_list = list(blacklisted_groups)
        for idx, nama in enumerate(blacklist_list, 1):
            teks += f"{idx}. {nama}\n"
            # Gunakan index untuk referensi yang lebih aman
            buttons.append([Button.inline(f"Hapus {nama[:25]}", data=f"rmbl_{idx}".encode())])
        # Simpan mapping sementara untuk callback
        if not hasattr(list_blacklist, '_mapping'):
            list_blacklist._mapping = {}
        list_blacklist._mapping[event.sender_id] = blacklist_list
        await event.respond(teks, buttons=buttons)

@client.on(events.NewMessage(pattern='/status'))
async def cek_status(event):
    now = datetime.now()
    sisa = (MASA_AKTIF - now).days
    tanggal_akhir = MASA_AKTIF.strftime('%d %B %Y')
    await event.respond(
        f"Masa aktif tersisa: {sisa} hari\n"
        f"Userbot aktif sampai: {tanggal_akhir}"
    )

@client.on(events.NewMessage(pattern='/ping'))
async def ping(event):
    await event.respond("Bot aktif dan siap melayani!")

@client.on(events.NewMessage(pattern='/restart'))
async def restart(event):
    if not await require_allowed(event):
        return
    await event.respond("Bot akan restart...")
    logging.info("Restarting bot upon command...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@client.on(events.NewMessage(pattern='/log'))
async def log_handler(event):
    if not await require_allowed(event):
        return
    try:
        with open('bot.log','r',encoding='utf-8') as log_file:
            logs = log_file.read()
            if len(logs) > 4000:
                logs = logs[-4000:]
            await event.respond(f"Log Terbaru!:\n{logs}")
    except FileNotFoundError:
        await event.respond("Log tidak ditemukan bub :(")
    except Exception:
        await event.respond("Yahh ada masalah dalam membaca log.")

PENGEMBANG_USERNAME = "explicist"
@client.on(events.NewMessage(pattern=r'/feedback(?:\s+(.*))?'))
async def feedback_handler(event):
    msg = event.pattern_match.group(1)

    if not msg:
        return await event.reply(
            "Kirim saran/kritik atau pesan lain kepada pengembangku: /feedback <pesan>"
        )

    logging.info(f"Feedback dari {event.sender_id}: {msg}")

    # Balasan ke pengirim
    await event.reply(
        "Terima kasih atas feedback-nya!\n"
        "Masukanmu sangat berarti dan akan kami baca dengan penuh cinta!"
    )

    # Buat format feedback
    sender = await event.get_sender()
    name = sender.first_name if sender else "Tidak diketahui"
    username = f"@{sender.username}" if sender and sender.username else "Tidak ada"
    user_id = sender.id if sender else "Tidak diketahui"
    message = msg

    feedback_text = (
        "Feedback Baru!\n\n"
        f"Dari: {name}\n"
        f"Username: {username}\n"
        f"ID: {user_id}\n"
        f"Pesan: {message}"
    )

    # Kirim feedback ke pengembang
    try:
        await client.send_message(PENGEMBANG_USERNAME, feedback_text)
    except Exception as e:
        await event.reply("Ups! Gagal mengirim feedback ke pengembang. Coba lagi nanti yaaw.")
        print(f"[Feedback Error] {e}")

@client.on(events.NewMessage(pattern=r'/reply (\d+)\s+([\s\S]+)', from_users=PENGEMBANG_USERNAME))
async def reply_to_user(event):
    match = event.pattern_match
    user_id = int(match.group(1))
    reply_message = match.group(2).strip()
    try:
        await client.send_message(user_id, f"Pesan dari pengembang:\n\n{reply_message}")
        await event.reply("Balasanmu sudah dikirim ke pengguna!")
    except Exception as e:
        await event.reply("Gagal mengirim balasan ke pengguna. Mungkin user sudah block bot?")
        print(f"[Reply Error] {e}")

@client.on(events.NewMessage(pattern=r'^/help\b'))
async def help_cmd(event):
    teks = """
PANDUAN USERBOT HEARTIE
Hai, sayang! Aku Heartie, userbot-mu yang siap membantu menyebarkan pesan cinta ke semua grup-grup favoritmu. Berikut daftar perintah yang bisa kamu gunakan:
============================
/forward
Kirim pesan langsung ke grup.
— Mode forward (dari channel):
 /forward forward @namachannel jumlah_grup id_pesan jeda detik durasi_menit jumlah_pesan_perhari
 Contoh: /forward forward @usnchannel 50 27 5 180 300
 Untuk multiple pesan: /forward forward @channel id1||id2||id3 50 5 180 300
— Mode text (kirim teks langsung):
 /forward text "Halo semua!" jumlah_grup jeda detik durasi_menit jumlah_pesan_perhari
 Contoh: /forward text "Halo semua!" 10 5 180 300
 Untuk multiple pesan: /forward text "Pesan 1"||"Pesan 2"||"Pesan 3" 10 5 180 300
============================
2. /scheduleforward
 Jadwalkan pesan mingguan otomatis.
 — Format:
 /scheduleforward mode pesan/sumber jumlah_grup durasi_menit jeda jumlah_pesan hari1,day2 jam:menit
 — Contoh:
 /scheduleforward forward @usnchannel 20 120 5 300 senin,jumat 08:00
 /scheduleforward text "Halo dari bot!" 30 180 5 300 selasa,rabu 10:00
 Untuk multiple pesan: /scheduleforward text "Pesan 1"||"Pesan 2" 30 180 5 300 senin 08:00
============================
3. Manajemen Preset & Pesan
 — /review_pesan — Lihat pesan default
 — /ubah_pesan — Ubah pesan default
 — /simpan_preset — Simpan preset pesan
 — /pakai_preset — Pilih preset sebagai pesan default
 — /list_preset — Tampilkan daftar preset
 — /edit_preset — Edit preset pesan
 — /hapus_preset — Hapus preset
============================
4. Pengaturan Job Forward & Delay
 — /review — Tampilkan jadwal aktif
 — /deletejob — Hapus jadwal forward
 — /setdelay — Atur jeda antar batch kirim
 — /stopforward — Hentikan semua job forward aktif kamu (termasuk yang sedang berjalan)
 — /setdelaygroup 5 — Set delay antar grup ke 5 detik (bisa diubah)
 — /cekdelaygroup — Cek delay antar grup kamu saat ini
 — /resetdelaygroup — Reset delay antar grup ke default
============================
5. Blacklist Grup
 — /blacklist_add — Tambahkan grup ke blacklist
 — /blacklist_remove — Hapus grup dari blacklist
 — /list_blacklist — Lihat daftar grup dalam blacklist
============================
6. User Allowed
 — /adduserbutton — Menambahkan daftar user yang diizinkan memakai userbot
 — /listuser — Menampilkan daftar user yang diizinkan memakai userbot
============================
7. Info & Lain-lain
 — /status — Cek masa aktif userbot
 — /ping — Periksa apakah bot aktif
 — /log — Tampilkan log aktivitas bot
 — /feedback — Kirim feedback ke pengembang
 — /stats — Lihat statistik penggunaan forward
 — /restart — Restart bot
============================
Cara mendapatkan ID pesan channel:
Klik kanan bagian kosong (atau tap lama) pada pesan di channel → Salin link.
Misal, jika linknya https://t.me/channel/19 maka id pesan adalah 19.
Selamat mencoba dan semoga hari-harimu penuh cinta! Kalau masih ada yang bingung bisa chat pengembangku/kirimkan feedback ya!
"""
    await event.respond(teks)

@client.on(events.NewMessage(pattern='/info'))
async def info_handler(event):
    if not await is_allowed(event):
        return
    now = datetime.now()
    uptime = now - start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    aktif_sejak = start_time.strftime("%d %B %Y pukul %H:%M WIB")

    text = (
        "Tentang Heartie Bot\n\n"
        "Haiii! Aku Heartie, sahabatmu yang selalu setia meneruskan pesan penuh cinta ke grup-grup kesayanganmu~\n\n"
        "Dibuat oleh: @explicist\n"
        "Versi: 1.2.0\n"
        "Ditenagai oleh: Python + Telethon\n"
        "Fungsi: Kirim & jadwalkan pesan otomatis ke banyak grup\n\n"
        f"Uptime: {hours} jam, {minutes} menit\n"
        f"Aktif sejak: {aktif_sejak}\n\n"
        "Butuh bantuan? Yuk klik tombol di bawah ini~"
    )

    buttons = [
        [Button.inline("Panduan /help", data=b"show_help")],
        [Button.inline("Lihat Statistik", data=b"refresh_stats")],
        [Button.url("Hubungi Pembuat", url="https://t.me/explicist")]
    ]

    await event.respond(text, buttons=buttons, parse_mode='markdown')

@client.on(events.CallbackQuery)
async def callback_handler(event):
    if not await is_allowed(event):
        return
    if event.data == b"refresh_stats":
        await stats_handler(event)
        await event.answer("Statistik diperbarui!")
    
    elif event.data == b"show_help":
        await help_cmd(event)
        await event.answer("Ini panduannya~")
    
    elif event.data == b"download_log":
        try:
            await client.send_file(event.chat_id, 'bot.log', caption="Ini dia log-nya yaa!")
            await event.answer("Log dikirim!")
        except FileNotFoundError:
            await event.answer("Log belum tersedia~", alert=True)
    
    elif event.data == b"ubah_pesan_menu":
        await event.answer("Gunakan: /ubah_pesan <pesan baru>", alert=True)
    
    elif event.data.startswith(b"delete_"):
        # Handle delete job button
        try:
            job_id = event.data[7:].decode()  # Remove "delete_" prefix
            user_id = event.sender_id
            
            # Verify job belongs to user
            if job_id in job_data and job_data[job_id].get('user') == user_id:
                scheduler.remove_job(job_id)
                job_data.pop(job_id, None)
                JOBS.pop(job_id, None)
                await event.edit("Jadwal berhasil dihapus!")
                await event.answer("Jadwal dihapus!")
            else:
                await event.answer("Jadwal tidak ditemukan atau bukan milik Anda!", alert=True)
        except Exception as e:
            logging.error(f"Error delete job via button: {e}")
            await event.answer("Gagal menghapus jadwal!", alert=True)
    
    elif event.data.startswith(b"pakai_"):
        # Handle pakai preset button: pakai_{user_id}_{nama_preset}
        try:
            data_parts = event.data[6:].decode().split('_', 1)  # Remove "pakai_" prefix
            if len(data_parts) >= 2:
                preset_user_id = int(data_parts[0])
                nama_preset = data_parts[1]
                
                if preset_user_id == event.sender_id:
                    if preset_user_id in preset_pesan and nama_preset in preset_pesan[preset_user_id]:
                        pesan_simpan[preset_user_id] = preset_pesan[preset_user_id][nama_preset]
                        await event.edit(f"Preset '{nama_preset}' telah dipilih sebagai pesan default!")
                        await event.answer("Preset dipilih!")
                    else:
                        await event.answer("Preset tidak ditemukan!", alert=True)
                else:
                    await event.answer("Anda tidak memiliki akses!", alert=True)
        except Exception as e:
            logging.error(f"Error pakai preset via button: {e}")
            await event.answer("Gagal memilih preset!", alert=True)
    
    elif event.data.startswith(b"hapus_"):
        # Handle hapus preset button: hapus_{user_id}_{nama_preset}
        try:
            data_parts = event.data[6:].decode().split('_', 1)  # Remove "hapus_" prefix
            if len(data_parts) >= 2:
                preset_user_id = int(data_parts[0])
                nama_preset = data_parts[1]
                
                if preset_user_id == event.sender_id:
                    if preset_user_id in preset_pesan and nama_preset in preset_pesan[preset_user_id]:
                        del preset_pesan[preset_user_id][nama_preset]
                        await event.edit(f"Preset '{nama_preset}' berhasil dihapus!")
                        await event.answer("Preset dihapus!")
                    else:
                        await event.answer("Preset tidak ditemukan!", alert=True)
                else:
                    await event.answer("Anda tidak memiliki akses!", alert=True)
        except Exception as e:
            logging.error(f"Error hapus preset via button: {e}")
            await event.answer("Gagal menghapus preset!", alert=True)
    
    elif event.data.startswith(b"rmbl_"):
        # Handle remove blacklist button: rmbl_{index}
        try:
            idx = int(event.data[5:].decode())  # Remove "rmbl_" prefix
            user_id = event.sender_id
            
            # Get mapping from function attribute
            if hasattr(list_blacklist, '_mapping') and user_id in list_blacklist._mapping:
                blacklist_list = list_blacklist._mapping[user_id]
                if 1 <= idx <= len(blacklist_list):
                    nama = blacklist_list[idx - 1]
                    blacklisted_groups.discard(nama)
                    await event.edit(f"'{nama}' telah dihapus dari blacklist!")
                    await event.answer("Grup dihapus dari blacklist!")
                    # Clean up mapping
                    del list_blacklist._mapping[user_id]
                else:
                    await event.answer("Index tidak valid!", alert=True)
            else:
                # Fallback: coba hapus langsung dengan nama dari blacklist
                blacklist_list = list(blacklisted_groups)
                if 1 <= idx <= len(blacklist_list):
                    nama = blacklist_list[idx - 1]
                    blacklisted_groups.discard(nama)
                    await event.edit(f"'{nama}' telah dihapus dari blacklist!")
                    await event.answer("Grup dihapus dari blacklist!")
                else:
                    await event.answer("Grup tidak ditemukan!", alert=True)
        except Exception as e:
            logging.error(f"Error remove blacklist via button: {e}")
            await event.answer("Gagal menghapus dari blacklist!", alert=True)

@client.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    if not await is_allowed(event):
        return
    try:
        global TOTAL_SENT_MESSAGES

        sender = await event.get_sender()
        name = sender.first_name or "Pengguna"
        username = f"@{sender.username}" if sender.username else "(tanpa username)"
        chat_id = event.chat_id

        try:
            with open('allowed_users.txt', 'r') as f:
                total_users = len(f.readlines())
        except FileNotFoundError:
            total_users = 1

        stats_text = (
            f"Haii {name} ({username})!\n\n"
            "Statistik Heartie Bot:\n"
            f"Total job aktif: {len(JOBS)}\n"
            f"Total pesan terkirim: {TOTAL_SENT_MESSAGES}\n"
            f"Total pengguna terdaftar: {total_users}\n"
            f"Waktu server sekarang: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        buttons = [
            [Button.inline("Refresh", data=b"refresh_stats")],
            [Button.inline("Download Log", data=b"download_log")]
        ]

        await event.respond(stats_text, buttons=buttons, parse_mode='markdown')
        TOTAL_SENT_MESSAGES += 1
    except Exception as e:
        await event.respond(f"Terjadi kesalahan: {e}")

@client.on(events.NewMessage(pattern=r'/adduserbutton'))
async def add_user_button(event):
    if not await require_allowed(event):
        return await event.reply("Kamu nggak punya izin buat ini, ciyee.")

    user = await client.get_me()

    keyboard = [
        [Button.inline("Izinkan Saya Sendiri", data=f"add_{user.id}".encode())]
    ]

    await event.reply(
        "Pilih siapa yang mau kamu izinkan jadi pengguna bot ini yaa:",
        buttons=keyboard
    )

@client.on(events.CallbackQuery(data=re.compile(rb'add_(\d+)')))
async def handler_add_button(event):
    if not await require_allowed(event):
        return await event.answer("Kamu nggak punya izin!", alert=True)

    add_id = int(event.data_match.group(1))

    if add_id in ALLOWED_USERS:
        return await event.answer("User ini sudah diizinkan sebelumnya kok!", alert=True)

    ALLOWED_USERS.add(add_id)
    save_allowed_users()

    try:
        user = await client.get_entity(add_id)
        nama = user.first_name or "Tanpa Nama"
        username = f"@{user.username}" if user.username else "(tanpa username)"
    except Exception:
        nama = "Tidak diketahui"
        username = "(tanpa username)"

    await event.edit(
        f"Yey! User ini sekarang sudah jadi bagian dari Heartie Club!\n\n"
        f"Nama: {nama}\n"
        f"Username: {username}\n"
        f"ID: {add_id}"
    )

@client.on(events.NewMessage(pattern=r'/listuser'))
async def list_users(event):
    if not await is_allowed(event):
        return

    if not ALLOWED_USERS:
        return await event.reply("Belum ada user yang diizinkan, sayang...")

    teks = "Daftar pengguna yang diizinkan:\n\n"
    buttons = []

    for uid in sorted(ALLOWED_USERS):
        try:
            user = await client.get_entity(uid)
            nama = user.first_name or "Tanpa Nama"
            username = f"@{user.username}" if user.username else "(tanpa username)"
        except Exception:
            nama = "Tidak diketahui"
            username = "(tanpa username)"

        teks += f"{nama} | {username} | {uid}\n"
        buttons.append([Button.inline(f"Hapus {uid}", data=f"remove_{uid}".encode())])

    await event.reply(teks, buttons=buttons)

@client.on(events.CallbackQuery(data=re.compile(rb'remove_(\d+)')))
async def handler_remove_button(event):
    if not await require_allowed(event):
        return await event.answer("Kamu nggak punya izin!", alert=True)

    remove_id = int(event.data_match.group(1))

    if remove_id in ALLOWED_USERS:
        ALLOWED_USERS.remove(remove_id)
        save_allowed_users()
        try:
            user = await client.get_entity(remove_id)
            nama = user.first_name or "Tanpa Nama"
            username = f"@{user.username}" if user.username else "(tanpa username)"
        except Exception:
            nama = "Tidak diketahui"
            username = "(tanpa username)"

        await event.edit(
            f"{remove_id} telah dihapus dari daftar user yang diizinkan.\n"
            f"Nama: {nama}\n"
            f"Username: {username}"
        )
    else:
        await event.answer("User sudah tidak ada di daftar!", alert=True)

# === PENGECEKAN LISENSI ===
async def cek_lisensi():
    if datetime.now() > MASA_AKTIF:
        logging.error("Lisensi expired. Bot dihentikan.")
        sys.exit("Lisensi expired.")

# === SETUP FLASK UNTUK KEEP ALIVE ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Heartie Bot is alive!"

@app.route('/ping')
def ping():
    return "Xixi! Bot masih hidup."

def keep_alive():
    port = int(os.environ.get('PORT', '8000'))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=keep_alive, daemon=True).start()

# === JALANKAN BOT ===
async def main():
    await client.start()
    scheduler.start()
    await cek_lisensi()
    me = await client.get_me()
    welcome_msg = f"💖 Bot aktif, kamu masuk sebagai {me.first_name}. Menunggu perintahmu, sayang!"
    print(welcome_msg)
    logging.info(welcome_msg)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
