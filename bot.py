import os, io, csv, asyncio, datetime as dt
from zoneinfo import ZoneInfo, available_timezones

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.voice_states = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_CFG = {"min_minutes": 15, "timezone": "Asia/Kolkata", "mode": "presence",
               "log_channel_id": None, "reminder_minutes": 10,
               "leave_admin_id": None, "leave_channel_id": None, "leave_approvers": []}
cfg_cache: dict[str, dict] = {}
row_cache: dict[tuple, dict] = {}      # (slot_id, date) -> attendance row
finalized: set[tuple] = set()
reminded: set[tuple] = set()


# ---------- helpers ----------
async def q(req):
    return (await asyncio.to_thread(req.execute)).data


async def get_cfg(gid) -> dict:
    gid = str(gid)
    if gid not in cfg_cache:
        r = await q(sb.table("guild_config").select("*").eq("guild_id", gid))
        cfg_cache[gid] = r[0] if r else {**DEFAULT_CFG, "guild_id": gid}
    return cfg_cache[gid]


def tm(s) -> dt.time:
    return dt.time.fromisoformat(str(s))


def parse_hm(s: str) -> dt.time:
    return dt.datetime.strptime(s.strip(), "%H:%M").time()


def applies(slot, d: dt.date) -> bool:
    if slot["slot_date"]:
        return slot["slot_date"] == d.isoformat()
    if slot["weekday"] is not None:
        return slot["weekday"] == d.weekday()
    return True


def slot_label(s) -> str:
    when = s["slot_date"] or ("Daily" if s["weekday"] is None else DAYS[s["weekday"]])
    return f"{when} {tm(s['start_time']).strftime('%H:%M')}-{tm(s['end_time']).strftime('%H:%M')}"


async def log(guild, cfg, text):
    cid = cfg.get("log_channel_id")
    ch = guild.get_channel(int(cid)) if cid else None
    if ch:
        try:
            await ch.send(text)
        except discord.HTTPException:
            pass


def available(member: discord.Member, mode: str) -> bool:
    online = member.status != discord.Status.offline
    in_voice = member.voice is not None and member.voice.channel is not None
    return {"presence": online, "voice": in_voice, "either": online or in_voice}[mode]


async def get_row(slot, d: dt.date) -> dict:
    k = (slot["id"], d.isoformat())
    if k not in row_cache:
        r = await q(sb.table("attendance").select("*").eq("slot_id", slot["id"]).eq("att_date", k[1]))
        if not r:
            r = await q(sb.table("attendance").insert({
                "guild_id": slot["guild_id"], "slot_id": slot["id"], "moderator_id": slot["moderator_id"],
                "user_id": slot["user_id"], "att_date": k[1]}))
        row_cache[k] = r[0]
    return row_cache[k]


async def save_row(row: dict, **changes):
    row.update(changes)
    await q(sb.table("attendance").update(changes).eq("id", row["id"]))


# ---------- tracker ----------
@tasks.loop(seconds=60)
async def tracker():
    for guild in bot.guilds:
        try:
            await track_guild(guild)
        except Exception as e:  # keep loop alive
            print(f"[tracker] {guild.id}: {e!r}")


async def track_guild(guild: discord.Guild):
    cfg = await get_cfg(guild.id)
    now = dt.datetime.now(ZoneInfo(cfg["timezone"]))
    today = now.date()
    iso = today.isoformat()
    for k in [k for k in row_cache if k[1] != iso]:
        row_cache.pop(k, None)
    finalized.intersection_update({k for k in finalized if k[1] == iso})
    reminded.intersection_update({k for k in reminded if k[1] == iso})

    slots = await q(sb.table("slots").select("*, moderators(name)").eq("guild_id", str(guild.id)))
    leaves = await q(sb.table("leaves").select("user_id,slot_ids").eq("guild_id", str(guild.id))
                     .eq("status", "approved").lte("start_date", iso).gte("end_date", iso))
    for s in slots:
        if not applies(s, today):
            continue
        if any(l["user_id"] == s["user_id"] and (not l["slot_ids"] or s["id"] in l["slot_ids"]) for l in leaves):
            row = await get_row(s, today)
            if row["status"] in ("pending", "absent"):
                await save_row(row, status="excused", method="leave", note="Approved leave",
                               marked_at=dt.datetime.now(dt.timezone.utc).isoformat())
            continue
        start, end = tm(s["start_time"]), tm(s["end_time"])
        key = (s["id"], iso)
        member = guild.get_member(int(s["user_id"]))
        name = s["moderators"]["name"]

        # reminder
        start_dt = dt.datetime.combine(today, start, tzinfo=now.tzinfo)
        mins_to = (start_dt - now).total_seconds() / 60
        if 0 < mins_to <= cfg["reminder_minutes"] and key not in reminded:
            reminded.add(key)
            if member:
                try:
                    await member.send(f"⏰ Your mod slot **{slot_label(s)}** in **{guild.name}** starts "
                                      f"in ~{int(mins_to)} min. Be available for {cfg['min_minutes']}+ min.")
                except discord.HTTPException:
                    pass

        if start <= now.time() < end:
            row = await get_row(s, today)
            if row["status"] in ("present", "excused"):
                continue
            if member and available(member, cfg["mode"]):
                minutes = row["minutes"] + 1
                await save_row(row, minutes=minutes)
                if minutes >= cfg["min_minutes"] and row["status"] != "present":
                    await save_row(row, status="present", method="auto",
                                   marked_at=dt.datetime.now(dt.timezone.utc).isoformat())
                    await log(guild, cfg, f"✅ **{name}** auto-marked **present** "
                                          f"({minutes} min) for slot {slot_label(s)}")
        elif now.time() >= end and key not in finalized:
            finalized.add(key)
            row = await get_row(s, today)
            if row["status"] == "pending":
                await save_row(row, status="absent", method="auto",
                               marked_at=dt.datetime.now(dt.timezone.utc).isoformat())
                await log(guild, cfg, f"❌ **{name}** marked **absent** — only {row['minutes']} min "
                                      f"in slot {slot_label(s)} (needed {cfg['min_minutes']})")


@tracker.before_loop
async def _wait():
    await bot.wait_until_ready()


RETENTION_DAYS = 30


@tasks.loop(hours=6)
async def cleanup():
    for guild in bot.guilds:
        try:
            cfg = await get_cfg(guild.id)
            today = dt.datetime.now(ZoneInfo(cfg["timezone"])).date()
            cutoff = (today - dt.timedelta(days=RETENTION_DAYS)).isoformat()
            await q(sb.table("attendance").delete().eq("guild_id", str(guild.id)).lt("att_date", cutoff))
            await q(sb.table("leaves").delete().eq("guild_id", str(guild.id)).lt("end_date", cutoff))
        except Exception as e:
            print(f"[cleanup] {guild.id}: {e!r}")


@cleanup.before_loop
async def _wait_cleanup():
    await bot.wait_until_ready()


# ---------- /mod ----------
mod_g = app_commands.Group(name="mod", description="Manage moderators",
                           default_permissions=discord.Permissions(manage_guild=True), guild_only=True)


@mod_g.command(name="add", description="Add a moderator to the attendance roster")
async def mod_add(i: discord.Interaction, user: discord.Member, name: str | None = None):
    await q(sb.table("moderators").upsert({"guild_id": str(i.guild_id), "user_id": str(user.id),
                                           "name": name or user.display_name},
                                          on_conflict="guild_id,user_id"))
    await i.response.send_message(f"Added {user.mention} as **{name or user.display_name}**.", ephemeral=True)


@mod_g.command(name="remove", description="Remove a moderator (deletes their slots & history)")
async def mod_remove(i: discord.Interaction, user: discord.Member):
    await q(sb.table("moderators").delete().eq("guild_id", str(i.guild_id)).eq("user_id", str(user.id)))
    await i.response.send_message(f"Removed {user.mention}.", ephemeral=True)


@mod_g.command(name="list", description="List moderators and their slots")
async def mod_list(i: discord.Interaction):
    mods = await q(sb.table("moderators").select("*").eq("guild_id", str(i.guild_id)))
    slots = await q(sb.table("slots").select("*").eq("guild_id", str(i.guild_id)))
    if not mods:
        return await i.response.send_message("No moderators yet. Use `/mod add`.", ephemeral=True)
    lines = []
    for m in mods:
        ms = [f"`#{s['id']}` {slot_label(s)}" for s in slots if s["moderator_id"] == m["id"]]
        lines.append(f"**{m['name']}** (<@{m['user_id']}>)\n" + ("\n".join(f"  • {x}" for x in ms) or "  • no slots"))
    await i.response.send_message("\n".join(lines)[:1990], ephemeral=True)


# ---------- /slot ----------
slot_g = app_commands.Group(name="slot", description="Manage time slots",
                            default_permissions=discord.Permissions(manage_guild=True), guild_only=True)


@slot_g.command(name="add", description="Add an availability slot (24h HH:MM, server timezone)")
@app_commands.choices(day=[app_commands.Choice(name="Every day", value="daily"),
                           app_commands.Choice(name="Weekdays (Mon-Fri)", value="weekdays"),
                           *[app_commands.Choice(name=d, value=str(n)) for n, d in enumerate(DAYS)]])
async def slot_add(i: discord.Interaction, user: discord.Member, start: str, end: str,
                   day: app_commands.Choice[str] | None = None, date: str | None = None):
    try:
        st, en = parse_hm(start), parse_hm(end)
        if st >= en:
            raise ValueError
    except ValueError:
        return await i.response.send_message("Use 24h `HH:MM` with start before end (no overnight slots; "
                                             "split them in two).", ephemeral=True)
    m = await q(sb.table("moderators").select("id").eq("guild_id", str(i.guild_id)).eq("user_id", str(user.id)))
    if not m:
        return await i.response.send_message("Add them first with `/mod add`.", ephemeral=True)
    base = {"guild_id": str(i.guild_id), "moderator_id": m[0]["id"], "user_id": str(user.id),
            "start_time": st.isoformat(), "end_time": en.isoformat(), "weekday": None, "slot_date": None}
    if date:
        try:
            base["slot_date"] = dt.date.fromisoformat(date).isoformat()
        except ValueError:
            return await i.response.send_message("Date must be YYYY-MM-DD.", ephemeral=True)
        rows = [base]
    elif day is None or day.value == "daily":
        rows = [base]
    elif day.value == "weekdays":
        rows = [{**base, "weekday": n} for n in range(5)]
    else:
        rows = [{**base, "weekday": int(day.value)}]
    await q(sb.table("slots").insert(rows))
    await i.response.send_message(f"Added {len(rows)} slot(s) for {user.mention}: {slot_label(rows[0])}"
                                  f"{' (+ other weekdays)' if len(rows) > 1 else ''}", ephemeral=True)


@slot_g.command(name="remove", description="Remove a slot by its # id")
async def slot_remove(i: discord.Interaction, slot_id: int):
    await q(sb.table("slots").delete().eq("guild_id", str(i.guild_id)).eq("id", slot_id))
    await i.response.send_message(f"Removed slot #{slot_id}.", ephemeral=True)


@slot_g.command(name="list", description="List all slots")
async def slot_list(i: discord.Interaction, user: discord.Member | None = None):
    r = sb.table("slots").select("*, moderators(name)").eq("guild_id", str(i.guild_id))
    if user:
        r = r.eq("user_id", str(user.id))
    slots = await q(r.order("id"))
    txt = "\n".join(f"`#{s['id']}` **{s['moderators']['name']}** — {slot_label(s)}" for s in slots)
    await i.response.send_message(txt[:1990] or "No slots.", ephemeral=True)


# ---------- /attendance ----------
att_g = app_commands.Group(name="attendance", description="View & mark attendance",
                           default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
ICON = {"present": "✅", "absent": "❌", "excused": "🟡", "pending": "⏳"}


@att_g.command(name="today", description="Today's attendance board")
async def att_today(i: discord.Interaction):
    cfg = await get_cfg(i.guild_id)
    today = dt.datetime.now(ZoneInfo(cfg["timezone"])).date()
    slots = await q(sb.table("slots").select("*, moderators(name)").eq("guild_id", str(i.guild_id)))
    lines = []
    for s in sorted([s for s in slots if applies(s, today)], key=lambda s: s["start_time"]):
        r = await get_row(s, today)
        lines.append(f"{ICON[r['status']]} **{s['moderators']['name']}** `#{s['id']}` "
                     f"{slot_label(s)} — {r['minutes']} min ({r['status']}{', ' + r['method'] if r['method'] else ''})")
    await i.response.send_message("\n".join(lines)[:1990] or "No slots today.", ephemeral=True)


@att_g.command(name="mark", description="Manually mark attendance for a slot")
@app_commands.choices(status=[app_commands.Choice(name=n, value=n) for n in ("present", "absent", "excused")])
async def att_mark(i: discord.Interaction, user: discord.Member, status: app_commands.Choice[str],
                   slot_id: int | None = None, date: str | None = None, note: str | None = None,
                   force: bool = False):
    cfg = await get_cfg(i.guild_id)
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    try:
        d = dt.date.fromisoformat(date) if date else now.date()
    except ValueError:
        return await i.response.send_message("Date must be YYYY-MM-DD.", ephemeral=True)
    slots = await q(sb.table("slots").select("*").eq("guild_id", str(i.guild_id)).eq("user_id", str(user.id)))
    slots = [s for s in slots if applies(s, d) and (slot_id is None or s["id"] == slot_id)]
    if not slots:
        return await i.response.send_message("No matching slot for that user/date.", ephemeral=True)
    if slot_id is None:  # pick the latest slot already started
        started = [s for s in slots if d < now.date() or tm(s["start_time"]) <= now.time()]
        slots = sorted(started or slots, key=lambda s: s["start_time"])[-1:]
    s = slots[0]
    if status.value == "present" and d == now.date():
        elapsed = (now - dt.datetime.combine(d, tm(s["start_time"]), tzinfo=tz)).total_seconds() / 60
        if elapsed < cfg["min_minutes"]:
            if not (force and i.permissions.administrator):
                return await i.response.send_message(
                    f"Too early: slot started {max(int(elapsed), 0)} min ago; manual **present** is allowed "
                    f"after {cfg['min_minutes']} min. (Admins can use `force:True`.)", ephemeral=True)
    row = await get_row(s, d)
    await save_row(row, status=status.value, method="manual", marked_by=str(i.user.id), note=note,
                   marked_at=dt.datetime.now(dt.timezone.utc).isoformat())
    await log(i.guild, cfg, f"{ICON[status.value]} {i.user.mention} manually marked {user.mention} "
                            f"**{status.value}** for {slot_label(s)} on {d}{f' — {note}' if note else ''}")
    await i.response.send_message(f"Marked {user.mention} **{status.value}** (slot #{s['id']}, {d}).", ephemeral=True)


async def fetch_range(guild_id, days: int):
    cfg = await get_cfg(guild_id)
    today = dt.datetime.now(ZoneInfo(cfg["timezone"])).date()
    since = (today - dt.timedelta(days=days)).isoformat()
    rows = await q(sb.table("attendance").select("*, moderators(name), slots(start_time,end_time)")
                   .eq("guild_id", str(guild_id)).gte("att_date", since).order("att_date"))
    return [r for r in rows if r["status"] != "pending"]


@att_g.command(name="report", description="Summary per moderator")
async def att_report(i: discord.Interaction, days: app_commands.Range[int, 1, 30] = 30):
    rows = await fetch_range(i.guild_id, days)
    stats: dict[str, dict] = {}
    for r in rows:
        s = stats.setdefault(r["moderators"]["name"], {"present": 0, "absent": 0, "excused": 0})
        s[r["status"]] += 1
    if not stats:
        return await i.response.send_message("No records yet.", ephemeral=True)
    out = [f"**Last {days} days**"]
    for n, s in sorted(stats.items()):
        counted = s["present"] + s["absent"]
        pct = f"{100 * s['present'] / counted:.0f}%" if counted else "n/a"
        out.append(f"**{n}** — {s['present']} Present · {s['absent']} Absent · {s['excused']} Excused || {pct} Attendance")
    await i.response.send_message("\n".join(out)[:1990], ephemeral=True)


@att_g.command(name="export", description="Export attendance as CSV")
async def att_export(i: discord.Interaction, days: app_commands.Range[int, 1, 30] = 30):
    rows = await fetch_range(i.guild_id, days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "moderator", "slot", "minutes", "status", "method", "note"])
    for r in rows:
        w.writerow([r["att_date"], r["moderators"]["name"],
                    f"{tm(r['slots']['start_time']):%H:%M}-{tm(r['slots']['end_time']):%H:%M}",
                    r["minutes"], r["status"], r["method"] or "", r["note"] or ""])
    await i.response.send_message(file=discord.File(io.BytesIO(buf.getvalue().encode()),
                                                    filename=f"attendance_{days}d.csv"), ephemeral=True)


# ---------- /config ----------
cfg_g = app_commands.Group(name="config", description="Bot settings",
                           default_permissions=discord.Permissions(manage_guild=True), guild_only=True)


@cfg_g.command(name="show", description="Show settings")
async def cfg_show(i: discord.Interaction):
    c = await get_cfg(i.guild_id)
    ch = f"<#{c['log_channel_id']}>" if c.get("log_channel_id") else "not set"
    await i.response.send_message(
        f"Min minutes: **{c['min_minutes']}**\nTimezone: **{c['timezone']}**\nMode: **{c['mode']}**\n"
        f"Log channel: {ch}\nReminder: **{c['reminder_minutes']}** min before slot\n"
        f"Leave channel: {('<#' + c['leave_channel_id'] + '>') if c.get('leave_channel_id') else 'not set (uses log channel)'}\n"
        f"Leave approvers: {fmt_approvers(c.get('leave_approvers'))} (+ anyone with Manage Server)",
        ephemeral=True)


@cfg_g.command(name="set", description="Change settings (only fill what you want to change)")
@app_commands.choices(mode=[app_commands.Choice(name="Online status", value="presence"),
                            app_commands.Choice(name="In a voice channel", value="voice"),
                            app_commands.Choice(name="Online OR in voice", value="either")])
async def cfg_set(i: discord.Interaction, min_minutes: app_commands.Range[int, 1, 600] | None = None,
                  timezone: str | None = None, mode: app_commands.Choice[str] | None = None,
                  log_channel: discord.TextChannel | None = None,
                  reminder_minutes: app_commands.Range[int, 0, 120] | None = None,
                  leave_channel: discord.TextChannel | None = None):
    upd = {}
    if min_minutes: upd["min_minutes"] = min_minutes
    if timezone:
        if timezone not in available_timezones():
            return await i.response.send_message("Unknown timezone, e.g. `Asia/Kolkata`.", ephemeral=True)
        upd["timezone"] = timezone
    if mode: upd["mode"] = mode.value
    if log_channel: upd["log_channel_id"] = str(log_channel.id)
    if reminder_minutes is not None: upd["reminder_minutes"] = reminder_minutes
    if leave_channel: upd["leave_channel_id"] = str(leave_channel.id)
    if not upd:
        return await i.response.send_message("Nothing to change.", ephemeral=True)
    c = {**await get_cfg(i.guild_id), **upd, "guild_id": str(i.guild_id)}
    await q(sb.table("guild_config").upsert(c, on_conflict="guild_id"))
    cfg_cache[str(i.guild_id)] = c
    await i.response.send_message("Settings updated.", ephemeral=True)


async def save_cfg(gid, upd: dict):
    c = {**await get_cfg(gid), **upd, "guild_id": str(gid)}
    await q(sb.table("guild_config").upsert(c, on_conflict="guild_id"))
    cfg_cache[str(gid)] = c


@cfg_g.command(name="approver_add", description="Let a role or member approve/reject leave requests")
async def approver_add(i: discord.Interaction, target: discord.Member | discord.Role):
    key = ("role:" if isinstance(target, discord.Role) else "user:") + str(target.id)
    lst = list((await get_cfg(i.guild_id)).get("leave_approvers") or [])
    if key not in lst:
        lst.append(key)
    await save_cfg(i.guild_id, {"leave_approvers": lst})
    await i.response.send_message(f"Approvers: {fmt_approvers(lst)}", ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


@cfg_g.command(name="approver_remove", description="Remove a role or member from leave approvers")
async def approver_remove(i: discord.Interaction, target: discord.Member | discord.Role):
    key = ("role:" if isinstance(target, discord.Role) else "user:") + str(target.id)
    lst = [k for k in (await get_cfg(i.guild_id)).get("leave_approvers") or [] if k != key]
    await save_cfg(i.guild_id, {"leave_approvers": lst})
    await i.response.send_message(f"Approvers: {fmt_approvers(lst)}", ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


# ---------- moderator self-service ----------
@tree.command(name="myslots", description="See your own slots")
@app_commands.guild_only()
async def myslots(i: discord.Interaction):
    slots = await q(sb.table("slots").select("*").eq("guild_id", str(i.guild_id)).eq("user_id", str(i.user.id)))
    await i.response.send_message("\n".join(f"• {slot_label(s)}" for s in slots) or "You have no slots.",
                                  ephemeral=True)


@tree.command(name="mystats", description="Your attendance over the last 30 days")
@app_commands.guild_only()
async def mystats(i: discord.Interaction):
    gid, uid = str(i.guild_id), str(i.user.id)
    mod = await q(sb.table("moderators").select("id,name").eq("guild_id", gid).eq("user_id", uid))
    if not mod:
        return await i.response.send_message(
            "This Discord account isn't on the moderator roster. Ask an admin to run `/mod add` for you.",
            ephemeral=True)
    cfg = await get_cfg(i.guild_id)
    today = dt.datetime.now(ZoneInfo(cfg["timezone"])).date()
    since = (today - dt.timedelta(days=30)).isoformat()
    rows = await q(sb.table("attendance").select("*, slots(start_time,end_time)")
                   .eq("guild_id", gid).eq("user_id", uid).gte("att_date", since)
                   .order("att_date", desc=True).order("slot_id", desc=True))
    c = {k: sum(r["status"] == k for r in rows) for k in ("present", "absent", "excused", "pending")}
    pct = f"{100 * c['present'] / (c['present'] + c['absent']):.0f}%" if c["present"] + c["absent"] else "n/a"
    out = [f"**{mod[0]['name']}** — last 30 days: ✅{c['present']} ❌{c['absent']} 🟡{c['excused']}"
           f"{' ⏳' + str(c['pending']) + ' in progress' if c['pending'] else ''} · attendance {pct}"]
    for r in rows[:8]:
        sl = r["slots"]
        out.append(f"{ICON[r['status']]} {r['att_date']} {tm(sl['start_time']):%H:%M}-{tm(sl['end_time']):%H:%M}"
                   f" — {r['minutes']} min ({r['status']})")
    if not rows:
        out.append("No attendance records yet for your slots.")
    await i.response.send_message("\n".join(out)[:1990], ephemeral=True)

# ---------- leave requests ----------
def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s.strip())


async def describe_leave(L: dict) -> str:
    d = L["start_date"] if L["start_date"] == L["end_date"] else f"{L['start_date']} → {L['end_date']}"
    if L.get("slot_ids"):
        sl = await q(sb.table("slots").select("*").in_("id", L["slot_ids"]))
        scope = "Slots: " + ", ".join(slot_label(s) for s in sl)
    else:
        scope = "Whole day(s)"
    return f"📅 {d}\n🕒 {scope}"


async def apply_leave_to_existing(L: dict):
    """Excuse already-created rows (pending/absent) in range; future rows are handled by the tracker."""
    req = (sb.table("attendance").update({"status": "excused", "method": "leave", "note": "Approved leave"})
           .eq("guild_id", L["guild_id"]).eq("user_id", L["user_id"])
           .gte("att_date", L["start_date"]).lte("att_date", L["end_date"])
           .in_("status", ["pending", "absent"]))
    if L.get("slot_ids"):
        req = req.in_("slot_id", L["slot_ids"])
    await q(req)
    row_cache.clear()


async def decide_leave(i: discord.Interaction, lid: int, approve: bool):
    r = await q(sb.table("leaves").select("*, moderators(name)").eq("id", lid))
    if not r:
        return await i.response.send_message("This request no longer exists.", ephemeral=True)
    L = r[0]
    guild = bot.get_guild(int(L["guild_id"]))
    member = guild.get_member(i.user.id) if guild else None
    if not member or not can_decide(member, await get_cfg(guild.id)):
        return await i.response.send_message("You're not an approver for leave requests.", ephemeral=True)
    if L["status"] != "pending":
        return await i.response.send_message(f"Already **{L['status']}**.", ephemeral=True)
    status = "approved" if approve else "rejected"
    await q(sb.table("leaves").update({"status": status, "decided_by": str(i.user.id),
                                       "decided_at": dt.datetime.now(dt.timezone.utc).isoformat()}).eq("id", lid))
    if approve:
        await apply_leave_to_existing(L)
    mark = "✅ Approved" if approve else "❌ Rejected"
    await i.response.edit_message(content=f"{i.message.content}\n\n{mark} by {i.user.mention}", view=None)
    desc = await describe_leave(L)
    try:
        u = await bot.fetch_user(int(L["user_id"]))
        await u.send(f"Your leave request #{lid} in **{guild.name}** was **{status}**.\n{desc}")
    except discord.HTTPException:
        pass
    await log(guild, await get_cfg(guild.id),
              f"{mark.split()[0]} Leave #{lid} for **{L['moderators']['name']}** {status} by {i.user.mention}\n{desc}")


class LeaveBtn(discord.ui.DynamicItem[discord.ui.Button],
               template=r"leave:(?P<act>approve|reject):(?P<id>\d+)"):
    def __init__(self, act: str, lid: int):
        ok = act == "approve"
        super().__init__(discord.ui.Button(label="Approve" if ok else "Reject",
                                           style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
                                           custom_id=f"leave:{act}:{lid}"))
        self.act, self.lid = act, lid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await decide_leave(interaction, self.lid, self.act == "approve")


def can_decide(m: discord.Member, cfg: dict) -> bool:
    if m.guild_permissions.manage_guild:
        return True
    ids = set(cfg.get("leave_approvers") or [])
    return f"user:{m.id}" in ids or any(f"role:{r.id}" in ids for r in m.roles)


def fmt_approvers(lst) -> str:
    return " ".join(f"<@&{k[5:]}>" if k.startswith("role:") else f"<@{k[5:]}>" for k in lst or []) or "none"


async def notify_admin(guild: discord.Guild, cfg: dict, L: dict, name: str) -> bool:
    ch_id = cfg.get("leave_channel_id") or cfg.get("log_channel_id")
    ch = guild.get_channel(int(ch_id)) if ch_id else None
    if not ch:
        return False
    view = discord.ui.View(timeout=None)
    view.add_item(LeaveBtn("approve", L["id"]))
    view.add_item(LeaveBtn("reject", L["id"]))
    pings = fmt_approvers(cfg.get("leave_approvers"))
    text = (f"{pings + chr(10) if pings != 'none' else ''}"
            f"📝 **Leave request #{L['id']}** — **{name}** (<@{L['user_id']}>)\n"
            f"{await describe_leave(L)}\n💬 {L['reason'] or 'No reason given'}")
    try:
        await ch.send(text, view=view, allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        return True
    except discord.HTTPException:
        return False


async def submit_leave(guild: discord.Guild, user: discord.abc.User, mod: dict, start: dt.date,
                       end: dt.date, slot_ids: list[int] | None, reason: str):
    r = await q(sb.table("leaves").insert({
        "guild_id": str(guild.id), "moderator_id": mod["id"], "user_id": str(user.id),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "slot_ids": slot_ids, "reason": reason}))
    L = r[0]
    delivered = await notify_admin(guild, await get_cfg(guild.id), L, mod["name"])
    return L, delivered


def sent_msg(L, delivered):
    return (f"Leave request **#{L['id']}** submitted and posted for approval." if delivered else
            f"Leave request **#{L['id']}** saved, but no leave channel is set. Ask an admin to run "
            f"`/config set leave_channel`.")


class SlotPick(discord.ui.View):
    def __init__(self, guild, user, mod, slots, reason, today):
        super().__init__(timeout=120)
        self.guild, self.user, self.mod, self.reason, self.today = guild, user, mod, reason, today
        self.sel = discord.ui.Select(placeholder="Choose slot(s) for today", min_values=1,
                                     max_values=len(slots),
                                     options=[discord.SelectOption(label=slot_label(s)[:100], value=str(s["id"]))
                                              for s in slots])
        self.sel.callback = self.done
        self.add_item(self.sel)

    async def done(self, i: discord.Interaction):
        L, ok = await submit_leave(self.guild, self.user, self.mod, self.today, self.today,
                                   [int(v) for v in self.sel.values], self.reason)
        await i.response.edit_message(content=sent_msg(L, ok), view=None)


leave_g = app_commands.Group(name="leave", description="Leave requests", guild_only=True)


@leave_g.command(name="apply", description="Request leave: whole day, date range, or today's slots")
@app_commands.choices(leave_type=[app_commands.Choice(name="Whole day", value="whole_day"),
                                  app_commands.Choice(name="Date range (2-3 days etc.)", value="date_range"),
                                  app_commands.Choice(name="Specific slots today", value="today_slots")])
@app_commands.describe(from_date="YYYY-MM-DD (whole day: defaults to today)", to_date="YYYY-MM-DD (date range only)")
async def leave_apply(i: discord.Interaction, leave_type: app_commands.Choice[str], reason: str,
                      from_date: str | None = None, to_date: str | None = None):
    gid, uid = str(i.guild_id), str(i.user.id)
    mod = await q(sb.table("moderators").select("id,name").eq("guild_id", gid).eq("user_id", uid))
    if not mod:
        return await i.response.send_message("You're not on the moderator roster.", ephemeral=True)
    cfg = await get_cfg(i.guild_id)
    now = dt.datetime.now(ZoneInfo(cfg["timezone"]))
    today = now.date()
    try:
        if leave_type.value == "today_slots":
            slots = await q(sb.table("slots").select("*").eq("guild_id", gid).eq("user_id", uid))
            slots = [s for s in sorted(slots, key=lambda s: s["start_time"])
                     if applies(s, today) and tm(s["end_time"]) > now.time()]
            if not slots:
                return await i.response.send_message("You have no upcoming slots today.", ephemeral=True)
            return await i.response.send_message("Which slots do you want leave for?", ephemeral=True,
                                                 view=SlotPick(i.guild, i.user, mod[0], slots[:25], reason, today))
        start = parse_date(from_date) if from_date else today
        end = start
        if leave_type.value == "date_range":
            if not (from_date and to_date):
                return await i.response.send_message("Date range needs `from_date` and `to_date`.", ephemeral=True)
            end = parse_date(to_date)
    except ValueError:
        return await i.response.send_message("Dates must be `YYYY-MM-DD`.", ephemeral=True)
    if start < today or end < start or (end - start).days > 13:
        return await i.response.send_message("Dates must be today or later, end ≥ start, max 14 days.",
                                             ephemeral=True)
    await i.response.defer(ephemeral=True)
    L, ok = await submit_leave(i.guild, i.user, mod[0], start, end, None, reason)
    await i.followup.send(sent_msg(L, ok), ephemeral=True)


@leave_g.command(name="list", description="Your recent leave requests")
async def leave_list(i: discord.Interaction):
    rows = await q(sb.table("leaves").select("*").eq("guild_id", str(i.guild_id))
                   .eq("user_id", str(i.user.id)).order("id", desc=True).limit(10))
    icon = {"pending": "⏳", "approved": "✅", "rejected": "❌", "cancelled": "🚫"}
    txt = "\n".join(f"{icon[r['status']]} `#{r['id']}` {r['start_date']}"
                    f"{'' if r['start_date'] == r['end_date'] else ' → ' + r['end_date']}"
                    f"{' (slots)' if r['slot_ids'] else ''} — {r['status']}" for r in rows)
    await i.response.send_message(txt or "No leave requests.", ephemeral=True)


@leave_g.command(name="cancel", description="Cancel one of your pending leave requests")
async def leave_cancel(i: discord.Interaction, leave_id: int):
    r = await q(sb.table("leaves").update({"status": "cancelled"}).eq("id", leave_id)
                .eq("user_id", str(i.user.id)).eq("guild_id", str(i.guild_id)).eq("status", "pending"))
    await i.response.send_message(f"Cancelled #{leave_id}." if r else
                                  "No pending request with that id is yours.", ephemeral=True)


for g in (mod_g, slot_g, att_g, cfg_g, leave_g):
    tree.add_command(g)


@bot.event
async def on_ready():
    bot.add_dynamic_items(LeaveBtn)
    await tree.sync()
    if not tracker.is_running():
        tracker.start()
    if not cleanup.is_running():
        cleanup.start()
    print(f"Logged in as {bot.user}")


bot.run(os.environ["DISCORD_TOKEN"])