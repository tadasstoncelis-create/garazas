import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
import flet as ft

try:
    import requests
except ImportError:
    requests = None


FIREBASE_DB_URL = "https://garazas-40bc3-default-rtdb.europe-west1.firebasedatabase.app"
POLL_INTERVAL_SEC = 2.0

TELEGRAM_BOT_TOKEN = "9140007116:AAHGyYJ0n3RH-4TcAvItMh6_jYv6B0D48pQ"
TELEGRAM_CHAT_IDS: list[str] = ["613542160", "6285645972"]

LUNCH_SLOTS = {"13:00", "13:30"}

DURATION_OPTIONS = [
    (30, "30 min"),
    (60, "1 val"),
    (90, "1,5 val"),
    (120, "2 val"),
    (180, "3 val"),
    (240, "4 val"),
]

STATUS_CYCLE = ["planned", "arrived", "done"]
STATUS_INFO = {
    "planned": ("Suplanuota", "#dbeafe", "#1e40af", "#93c5fd"),
    "arrived": ("Atvyko", "#ffedd5", "#9a3412", "#fdba74"),
    "done": ("Atlikta", "#dcfce7", "#166534", "#86efac"),
}


def resolve_data_dir() -> str:
    for env_key in ("FLET_APP_STORAGE_DATA", "LOCALAPPDATA", "TEMP", "TMPDIR"):
        base = os.environ.get(env_key)
        if base:
            path = os.path.join(base, "DarbuGrafikas")
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except Exception:
                pass
    fallback = os.path.join(os.getcwd(), "data_grafikas")
    os.makedirs(fallback, exist_ok=True)
    return fallback


DATA_DIR = resolve_data_dir()


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def shift_date(date_text: str, days: int) -> str:
    dt = datetime.strptime(date_text, "%Y-%m-%d").date() + timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def time_to_minutes(time_text: str) -> int:
    h, m = map(int, time_text.split(":"))
    return h * 60 + m


def minutes_to_time(mins: int) -> str:
    h = mins // 60
    m = mins % 60
    return f"{h:02d}:{m:02d}"


def current_slot_minutes() -> int:
    now = datetime.now()
    now_mins = now.hour * 60 + now.minute
    return int(math.ceil(now_mins / 30.0) * 30)


def clean_phone(phone_text: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", phone_text or "")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


def occupied_slots_for_task(task: dict) -> set[str]:
    start_str = task.get("time") or "09:00"
    duration = int(task.get("duration", 30) or 30)
    steps = max(1, duration // 30)
    start_m = time_to_minutes(start_str)
    return {minutes_to_time(start_m + i * 30) for i in range(steps)}


def duration_label(minutes: int) -> str:
    for mins, label in DURATION_OPTIONS:
        if mins == minutes:
            return label
    return f"{minutes} min"


def sanitize_room(room_code: str) -> str:
    safe_room = "".join(ch for ch in room_code.upper() if ch.isalnum() or ch in ("_", "-"))
    return safe_room or "BENDRAS"


def storage_path(room_code: str) -> str:
    return os.path.join(DATA_DIR, f"{sanitize_room(room_code)}.json")


def load_local_tasks(room_code: str) -> list[dict]:
    path = storage_path(room_code)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_local_tasks(room_code: str, tasks: list[dict]) -> None:
    path = storage_path(room_code)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def firebase_enabled() -> bool:
    return bool(FIREBASE_DB_URL.strip()) and requests is not None


def firebase_room_url(room_code: str) -> str:
    base = FIREBASE_DB_URL.strip().rstrip("/")
    return f"{base}/rooms/{sanitize_room(room_code)}/tasks.json"


def cloud_fetch_tasks(room_code: str) -> tuple[bool, list[dict]]:
    if not firebase_enabled():
        return False, load_local_tasks(room_code)
    try:
        resp = requests.get(firebase_room_url(room_code), timeout=6)
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            return True, []
        if isinstance(raw, dict):
            return True, [v for v in raw.values() if isinstance(v, dict)]
        if isinstance(raw, list):
            return True, [v for v in raw if isinstance(v, dict)]
        return True, []
    except Exception:
        return False, load_local_tasks(room_code)


def cloud_push_tasks(room_code: str, tasks: list[dict]) -> bool:
    save_local_tasks(room_code, tasks)
    if not firebase_enabled():
        return False
    try:
        payload = {t["id"]: t for t in tasks if t.get("id")}
        resp = requests.put(firebase_room_url(room_code), json=payload, timeout=6)
        resp.raise_for_status()
        return True
    except Exception:
        return False


def send_telegram_async(text: str) -> None:
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_ids = [str(c).strip() for c in TELEGRAM_CHAT_IDS if str(c).strip()]
    if not token or not chat_ids or requests is None:
        return

    def _worker():
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for cid in chat_ids:
            try:
                requests.post(url, json={"chat_id": cid, "text": text}, timeout=6)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def diff_remote_changes(old_tasks: list[dict], new_tasks: list[dict], current_user: str) -> list[str]:
    old_map = {t.get("id"): t for t in old_tasks if t.get("id")}
    new_map = {t.get("id"): t for t in new_tasks if t.get("id")}
    messages = []

    for tid, new_t in new_map.items():
        actor = new_t.get("updated_by") or new_t.get("by") or "Kolega"
        if actor.strip().lower() == current_user.strip().lower():
            continue

        client = new_t.get("client", "Užsakymas")
        t_date = new_t.get("date", "")
        t_time = new_t.get("time", "")
        st_label = STATUS_INFO.get(new_t.get("status", "planned"), STATUS_INFO["planned"])[0]

        if tid not in old_map:
            messages.append(f"{actor} pridėjo: {client} ({t_date} {t_time})")
        else:
            old_t = old_map[tid]
            if old_t.get("status") != new_t.get("status"):
                messages.append(f"{actor} pakeitė būseną į „{st_label}“: {client} ({t_time})")
            elif (
                old_t.get("time") != new_t.get("time")
                or old_t.get("date") != new_t.get("date")
                or old_t.get("duration") != new_t.get("duration")
            ):
                messages.append(f"{actor} perkėlė laiką: {client} -> {t_date} {t_time}")
            elif old_t != new_t:
                messages.append(f"{actor} atnaujino: {client} ({t_time})")

    for tid, old_t in old_map.items():
        if tid not in new_map:
            actor = old_t.get("updated_by") or old_t.get("by") or "Kolega"
            if actor.strip().lower() != current_user.strip().lower():
                messages.append(f"{actor} pašalino: {old_t.get('client', 'Užsakymas')} ({old_t.get('time', '')})")

    return messages


def work_slots() -> list[str]:
    slots = []
    for hour in range(9, 17):
        for slot in (f"{hour:02d}:00", f"{hour:02d}:30"):
            if slot not in LUNCH_SLOTS:
                slots.append(slot)
    return slots


def extra_morning_slots() -> list[str]:
    return ["08:00", "08:30"]


def extra_evening_slots() -> list[str]:
    return ["17:00", "17:30", "18:00", "18:30", "19:00"]


def selectable_slots(include_extra: bool) -> list[str]:
    if include_extra:
        return extra_morning_slots() + work_slots() + extra_evening_slots()
    return work_slots()


def slot_tag(slot: str) -> str:
    if slot in extra_morning_slots():
        return "Papildomas (nuo 08:00)"
    if slot in extra_evening_slots():
        return "Papildomas (17:00–19:00+)"
    return "Darbo laikas (09:00–17:00)"


def main(page: ft.Page):
    page.title = "Darbų Grafikas"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = "#f1f5f9"
    page.padding = 0
    page.scroll = ft.ScrollMode.AUTO

    state = {
        "user": "",
        "room": "",
        "date": today_str(),
        "tasks": [],
        "edit_id": None,
        "status": "planned",
        "show_extra": False,
        "swipe_dx": 0.0,
        "in_app": False,
        "stop_sync": False,
    }

    name_input = ft.TextField(
        label="Tavo vardas",
        value="",
        border_radius=10,
    )
    room_input = ft.TextField(
        label="Komandos kodas",
        value="",
        capitalization=ft.TextCapitalization.CHARACTERS,
        border_radius=10,
    )
    login_error = ft.Text("", color="#b91c1c", size=13)

    header_title = ft.Text("", size=18, weight=ft.FontWeight.BOLD, color="#ffffff")
    header_sub = ft.Text("", size=12, color="#93c5fd")
    sync_badge = ft.Text("", size=11, color="#86efac")
    date_label = ft.Text("", size=14, weight=ft.FontWeight.W_600, color="#0f172a")
    alert_banner_text = ft.Text("", size=13, weight=ft.FontWeight.W_600, color="#7c2d12")
    alert_banner = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=14, vertical=8),
        bgcolor="#ffedd5",
        border=ft.Border.all(1, "#fdba74"),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                alert_banner_text,
                ft.TextButton("Slėpti", on_click=lambda e: hide_banner()),
            ],
        ),
    )
    slots_column = ft.Column(spacing=8, expand=True)

    m_date = ft.TextField(label="Data (MMMM-MM-DD)", value=today_str(), border_radius=10)
    m_time = ft.Dropdown(label="Pradžios laikas (tik laisvi)", options=[], border_radius=10)
    m_duration = ft.Dropdown(
        label="Kiek užtruks darbas",
        options=[ft.dropdown.Option(str(mins), label) for mins, label in DURATION_OPTIONS],
        value="60",
        border_radius=10,
    )
    m_preview = ft.Text("", size=12, weight=ft.FontWeight.W_600, color="#1e40af")
    m_client = ft.TextField(
        label="Klientas / Automobilis *",
        border_radius=10,
    )
    m_phone = ft.TextField(
        label="Telefono numeris",
        keyboard_type=ft.KeyboardType.PHONE,
        border_radius=10,
    )
    m_notes = ft.TextField(
        label="Pastabos",
        multiline=True,
        min_lines=2,
        max_lines=3,
        border_radius=10,
    )
    modal_error = ft.Text("", color="#b91c1c", size=12)

    st_btn_planned = ft.Button("Suplanuota", height=36)
    st_btn_arrived = ft.Button("Atvyko", height=36)
    st_btn_done = ft.Button("Atlikta", height=36)

    def hide_banner():
        alert_banner.visible = False
        try:
            alert_banner.update()
        except Exception:
            page.update()

    def notify_user(msg: str, highlight: bool = False):
        if highlight:
            alert_banner_text.value = f"🔔 {msg}"
            alert_banner.visible = True
        page.snack_bar = ft.SnackBar(content=ft.Text(msg), open=True)
        page.update()

    async def call_client(raw_phone: str):
        tel = clean_phone(raw_phone)
        if not tel:
            notify_user("Nėra išsaugoto telefono numerio.", highlight=False)
            return
        try:
            await page.launch_url(f"tel:{tel}")
        except Exception:
            notify_user(f"Numeris: {tel}", highlight=False)

    def refresh_status_buttons():
        mapping = {
            "planned": st_btn_planned,
            "arrived": st_btn_arrived,
            "done": st_btn_done,
        }
        for key, btn in mapping.items():
            label, bg, fg, border_col = STATUS_INFO[key]
            selected = state["status"] == key
            btn.text = f"✓ {label}" if selected else label
            btn.bgcolor = bg if selected else "#f8fafc"
            btn.color = fg if selected else "#475569"
            btn.style = ft.ButtonStyle(
                side=ft.BorderSide(2 if selected else 1, fg if selected else border_col)
            )

    def set_modal_status(key: str):
        state["status"] = key
        refresh_status_buttons()
        page.update()

    st_btn_planned.on_click = lambda e: set_modal_status("planned")
    st_btn_arrived.on_click = lambda e: set_modal_status("arrived")
    st_btn_done.on_click = lambda e: set_modal_status("done")

    def get_blocked_slots(target_date: str, ignore_id: str | None = None) -> set[str]:
        blocked = set(LUNCH_SLOTS)
        for t in state["tasks"]:
            if t.get("date") == target_date and t.get("id") != ignore_id:
                blocked |= occupied_slots_for_task(t)
        return blocked

    def max_available_minutes(start_time: str, blocked: set[str]) -> int:
        start_m = time_to_minutes(start_time)
        max_end_m = 20 * 60
        steps = 0
        while True:
            slot_m = start_m + steps * 30
            if slot_m >= max_end_m:
                break
            if minutes_to_time(slot_m) in blocked:
                break
            steps += 1
        return max(30, steps * 30)

    def can_fit_duration(start_time: str, duration_mins: int, blocked: set[str]) -> bool:
        return duration_mins <= max_available_minutes(start_time, blocked)

    def update_preview_text(start_time: str | None, duration_mins: int, blocked: set[str]):
        if not start_time:
            m_preview.value = "Šią dieną tinkamų laisvų laikų nebėra."
            m_preview.color = "#b91c1c"
            return
        end_t = minutes_to_time(time_to_minutes(start_time) + duration_mins)
        max_m = max_available_minutes(start_time, blocked)
        max_end_t = minutes_to_time(time_to_minutes(start_time) + max_m)
        tag = slot_tag(start_time)
        if max_m < 240:
            m_preview.value = f"{tag}: {start_time}–{end_t} (maks. iki {max_end_t})"
            m_preview.color = "#b45309"
        else:
            m_preview.value = f"{tag}: {start_time}–{end_t}"
            m_preview.color = "#1e40af"

    def refresh_time_and_duration(target_date: str, preferred_time: str | None, preferred_duration: int, ignore_id: str | None):
        blocked = get_blocked_slots(target_date, ignore_id)
        include_extra = state["show_extra"] or (
            preferred_time is not None and preferred_time not in work_slots()
        )
        base_slots = selectable_slots(include_extra)

        is_new_record = ignore_id is None
        min_allowed_mins = current_slot_minutes() if (is_new_record and target_date == today_str()) else 0

        valid_start_times = []
        for s in base_slots:
            if s in LUNCH_SLOTS and s != preferred_time:
                continue
            if s == preferred_time:
                valid_start_times.append(s)
                continue
            if s in blocked:
                continue
            if is_new_record and time_to_minutes(s) < min_allowed_mins:
                continue
            valid_start_times.append(s)

        m_time.options = [ft.dropdown.Option(s, f"{s} • {slot_tag(s)}") for s in valid_start_times]

        if valid_start_times:
            chosen_start = preferred_time if preferred_time in valid_start_times else valid_start_times[0]
            m_time.value = chosen_start
            m_time.disabled = False
        else:
            chosen_start = None
            m_time.value = None
            m_time.disabled = True

        valid_durations = []
        if chosen_start:
            allowed_max = max_available_minutes(chosen_start, blocked)
            for mins, label in DURATION_OPTIONS:
                if mins <= allowed_max:
                    valid_durations.append((mins, label))

        if not valid_durations:
            valid_durations = [(30, "30 min")]

        m_duration.options = [ft.dropdown.Option(str(mins), label) for mins, label in valid_durations]
        allowed_mins = [mins for mins, _ in valid_durations]
        chosen_dur = preferred_duration if preferred_duration in allowed_mins else allowed_mins[-1]
        m_duration.value = str(chosen_dur)

        update_preview_text(chosen_start, chosen_dur, blocked)

        try:
            m_time.update()
            m_duration.update()
            m_preview.update()
        except Exception:
            pass

    def on_modal_inputs_change(e):
        chosen = (m_date.value or "").strip()
        try:
            datetime.strptime(chosen, "%Y-%m-%d")
            modal_error.value = ""
            refresh_time_and_duration(
                chosen,
                m_time.value,
                int(m_duration.value or "60"),
                state["edit_id"],
            )
        except ValueError:
            pass
        page.update()

    m_date.on_change = on_modal_inputs_change
    m_time.on_change = on_modal_inputs_change
    m_duration.on_change = on_modal_inputs_change

    def close_modal(e=None):
        task_dialog.open = False
        page.update()

    def persist_tasks(updated_tasks: list[dict], snack_msg: str, tg_msg: str | None = None):
        state["tasks"] = updated_tasks
        synced = cloud_push_tasks(state["room"], state["tasks"])
        sync_badge.value = "● Sinchronizuota (Firebase)" if synced else "● Vietinis režimas"
        if tg_msg:
            send_telegram_async(f"[{sanitize_room(state['room'])}] {tg_msg}")
        render_schedule()
        notify_user(snack_msg, highlight=False)

    def save_task_click(e):
        client = (m_client.value or "").strip()
        phone = (m_phone.value or "").strip()
        chosen_date = (m_date.value or "").strip()
        chosen_time = m_time.value
        duration_mins = int(m_duration.value or "60")
        notes = (m_notes.value or "").strip()

        if not client:
            modal_error.value = "Įrašykite klientą arba automobilį."
            page.update()
            return

        try:
            datetime.strptime(chosen_date, "%Y-%m-%d")
        except ValueError:
            modal_error.value = "Netinkamas datos formatas. Naudokite MMMM-MM-DD."
            page.update()
            return

        if not chosen_time:
            modal_error.value = "Nėra pasirinkto tinkamo laisvo laiko."
            page.update()
            return

        if chosen_time in LUNCH_SLOTS:
            modal_error.value = "13:00–14:00 yra pietų laikas."
            page.update()
            return

        if state["edit_id"] is None and chosen_date == today_str():
            if time_to_minutes(chosen_time) < current_slot_minutes():
                modal_error.value = "Naują įrašą šiandien galima daryti tik nuo esamo laiko ir į priekį."
                refresh_time_and_duration(chosen_date, None, duration_mins, None)
                page.update()
                return

        task_id = state["edit_id"] or f"t_{int(datetime.now().timestamp() * 1000)}"
        blocked = get_blocked_slots(chosen_date, task_id)

        if not can_fit_duration(chosen_time, duration_mins, blocked):
            modal_error.value = "Vėliau šią dieną jau yra užsakymas arba pietų laikas – pasirinkite trumpesnę trukmę."
            refresh_time_and_duration(chosen_date, chosen_time, 30, task_id)
            page.update()
            return

        existing_item = next((t for t in state["tasks"] if t.get("id") == task_id), None)
        creator = existing_item.get("by") if existing_item else state["user"]

        record = {
            "id": task_id,
            "date": chosen_date,
            "time": chosen_time,
            "duration": duration_mins,
            "client": client,
            "phone": phone,
            "notes": notes,
            "status": state["status"],
            "by": creator or state["user"],
            "updated_by": state["user"],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

        next_tasks = []
        replaced = False
        for item in state["tasks"]:
            if item.get("id") == task_id:
                next_tasks.append(record)
                replaced = True
            else:
                next_tasks.append(item)
        if not replaced:
            next_tasks.append(record)

        state["date"] = chosen_date
        task_dialog.open = False
        action_verb = "atnaujino" if existing_item else "pridėjo"
        phone_part = f", tel. {phone}" if phone else ""
        tg_text = f"{state['user']} {action_verb}: {client}{phone_part} ({chosen_date} {chosen_time}, {duration_label(duration_mins)})"
        persist_tasks(next_tasks, "Įrašas išsaugotas.", tg_msg=tg_text)

    def delete_task_click(e):
        if not state["edit_id"]:
            return
        removed = next((t for t in state["tasks"] if t.get("id") == state["edit_id"]), None)
        next_tasks = [t for t in state["tasks"] if t.get("id") != state["edit_id"]]
        task_dialog.open = False
        rem_client = removed.get("client", "Užsakymas") if removed else "Užsakymas"
        rem_date = removed.get("date", "") if removed else ""
        rem_time = removed.get("time", "") if removed else ""
        tg_text = f"{state['user']} pašalino: {rem_client} ({rem_date} {rem_time})"
        persist_tasks(next_tasks, "
