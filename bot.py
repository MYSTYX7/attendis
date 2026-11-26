import os, io, csv, asyncio, datetime as dt
from zoneinfo import ZoneInfo, available_timezones

import discord
from aiohttp import web, ClientSession, ClientTimeout
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

intents = discord.Intents.default()
intents.members = True
intents.presences = True
TRIGGER = (os.getenv("TEXT_TRIGGER") or "").strip().lower()   # e.g. "att" -> typing it opens the panel
intents.voice_states = True
intents.message_content = bool(TRIGGER)
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
            if row["status"] == "pending" or (row["status"] == "absent" and row.get("method") != "manual"):
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
        out.append(f"**{n}** — ✅{s['present']} ❌{s['absent']} 🟡{s['excused']} · attendance {pct}")
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

# ---------- optional keep-alive (for hosts that sleep idle VMs, e.g. Fly.io Sprites) ----------
async def self_hold(base: str):
    """Hold a long-lived request open to our own public URL so the host sees an active connection."""
    while True:
        try:
            async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=15)) as sess:
                async with sess.get(base.rstrip("/") + "/hold") as r:
                    async for _ in r.content.iter_any():
                        pass
        except Exception as e:
            print(f"[keepalive] {e!r}")
        await asyncio.sleep(5)


async def start_keepalive():
    base = os.getenv("KEEPALIVE_URL")
    if not base:
        return

    async def hold(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        try:
            while True:
                await resp.write(b".\n")
                await asyncio.sleep(20)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    app = web.Application()
    app.add_routes([web.get("/", lambda r: web.Response(text="ok")), web.get("/hold", hold)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    asyncio.create_task(self_hold(base))
    print(f"[keepalive] serving on :{os.getenv('PORT', '8080')}, holding {base}")


_keepalive_started = False


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
           .in_("status", ["pending", "absent"]).or_("method.is.null,method.neq.manual"))
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


# ---------- interactive control panel ----------
async def admin_only(i: discord.Interaction) -> bool:
    if i.user.guild_permissions.manage_guild:
        return True
    await i.response.send_message("🔒 You need the **Manage Server** permission for this button.", ephemeral=True)
    return False


async def panel_embed(guild: discord.Guild) -> discord.Embed:
    cfg = await get_cfg(guild.id)
    today = dt.datetime.now(ZoneInfo(cfg["timezone"])).date()
    slots = await q(sb.table("slots").select("*").eq("guild_id", str(guild.id)))
    rows = await q(sb.table("attendance").select("status").eq("guild_id", str(guild.id))
                   .eq("att_date", today.isoformat()))
    c = {k: sum(r["status"] == k for r in rows) for k in ("present", "pending", "absent", "excused")}
    e = discord.Embed(title="🛡️ ATTENDIS — ATTENDANCE PANEL", color=0x5865F2)
    e.description = (f"📅 **{today:%a, %d %b %Y}** · {cfg['timezone']}\n"
                     f"Slots today: **{sum(applies(s, today) for s in slots)}** · ✅ {c['present']} · "
                     f"⏳ {c['pending']} · ❌ {c['absent']} · 🟡 {c['excused']}\n"
                     f"Auto-present after **{cfg['min_minutes']} min** ({cfg['mode']})")
    e.set_footer(text="Attendis · attendance panel")
    return e


def pick_view(options, placeholder, on_pick) -> discord.ui.View:
    v = discord.ui.View(timeout=120)
    sel = discord.ui.Select(placeholder=placeholder, options=options[:25])

    async def cb(i: discord.Interaction):
        await on_pick(i, sel.values[0])
    sel.callback = cb
    v.add_item(sel)
    return v


async def roster_options(gid) -> list[discord.SelectOption]:
    mods = await q(sb.table("moderators").select("name,user_id").eq("guild_id", str(gid)).order("name"))
    return [discord.SelectOption(label=m["name"][:100], value=m["user_id"]) for m in mods]


async def today_slot_options(guild_id, today) -> tuple[list[discord.SelectOption], dict]:
    slots = await q(sb.table("slots").select("*, moderators(name)").eq("guild_id", str(guild_id)))
    slots = sorted([s for s in slots if applies(s, today)], key=lambda s: s["start_time"])
    return ([discord.SelectOption(label=f"{s['moderators']['name']} · {slot_label(s)}"[:100], value=str(s["id"]))
             for s in slots[:25]], {str(s["id"]): s for s in slots})


class SlotModal(discord.ui.Modal, title="Add slot"):
    start = discord.ui.TextInput(label="Start (24h HH:MM)", placeholder="09:00", max_length=5)
    end = discord.ui.TextInput(label="End (24h HH:MM)", placeholder="17:00", max_length=5)
    day = discord.ui.TextInput(label="Day", default="daily", max_length=10,
                               placeholder="daily / weekdays / mon..sun / 2026-12-01")

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, i: discord.Interaction):
        d = self.day.value.strip().lower()
        names = {"daily": "daily", "weekdays": "weekdays", **{n.lower(): str(k) for k, n in enumerate(DAYS)}}
        choice = app_commands.Choice(name=d, value=names[d]) if d in names else None
        await slot_add.callback(i, self.member, self.start.value, self.end.value, choice,
                                None if choice else d)


class StatusView(discord.ui.View):
    def __init__(self, slot: dict):
        super().__init__(timeout=120)
        self.slot = slot

    async def _go(self, i: discord.Interaction, status: str):
        member = i.guild.get_member(int(self.slot["user_id"]))
        if not member:
            return await i.response.send_message("That moderator isn't in the server.", ephemeral=True)
        await att_mark.callback(i, member, app_commands.Choice(name=status, value=status),
                                self.slot["id"], None, None, False)

    @discord.ui.button(label="Present", style=discord.ButtonStyle.success, emoji="✅")
    async def present(self, i, b): await self._go(i, "present")

    @discord.ui.button(label="Absent", style=discord.ButtonStyle.danger, emoji="❌")
    async def absent(self, i, b): await self._go(i, "absent")

    @discord.ui.button(label="Excused", style=discord.ButtonStyle.secondary, emoji="🟡")
    async def excused(self, i, b): await self._go(i, "excused")


class SettingsModal(discord.ui.Modal, title="Edit settings"):
    def __init__(self, c: dict):
        super().__init__()
        self.mins = discord.ui.TextInput(label="Minutes needed for present", default=str(c["min_minutes"]), max_length=3)
        self.tz = discord.ui.TextInput(label="Timezone", default=c["timezone"], max_length=40)
        self.mode = discord.ui.TextInput(label="Mode: presence / voice / either", default=c["mode"], max_length=8)
        self.rem = discord.ui.TextInput(label="Reminder minutes before slot", default=str(c["reminder_minutes"]), max_length=3)
        for x in (self.mins, self.tz, self.mode, self.rem):
            self.add_item(x)

    async def on_submit(self, i: discord.Interaction):
        m = self.mode.value.strip().lower()
        if not (self.mins.value.isdigit() and 1 <= int(self.mins.value) <= 600 and self.rem.value.isdigit()
                and 0 <= int(self.rem.value) <= 120 and m in ("presence", "voice", "either")):
            return await i.response.send_message("Invalid values (minutes 1-600, reminder 0-120, "
                                                 "mode presence/voice/either).", ephemeral=True)
        await cfg_set.callback(i, min_minutes=int(self.mins.value), timezone=self.tz.value.strip(),
                               mode=app_commands.Choice(name=m, value=m), reminder_minutes=int(self.rem.value))


class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Edit values", emoji="⚙️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, i, b): await i.response.send_modal(SettingsModal(await get_cfg(i.guild_id)))

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="📜 Log channel", row=1)
    async def log_ch(self, i, sel): await cfg_set.callback(i, log_channel=sel.values[0])

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="📝 Leave-requests channel", row=2)
    async def leave_ch(self, i, sel): await cfg_set.callback(i, leave_channel=sel.values[0])

    @discord.ui.select(cls=discord.ui.MentionableSelect, placeholder="✅ Leave approvers (roles/members)",
                       min_values=0, max_values=10, row=3)
    async def approvers(self, i, sel):
        keys = [("role:" if isinstance(v, discord.Role) else "user:") + str(v.id) for v in sel.values]
        await save_cfg(i.guild_id, {"leave_approvers": keys})
        await i.response.send_message(f"Approvers: {fmt_approvers(keys)} (+ anyone with Manage Server)",
                                      ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class LeaveModal(discord.ui.Modal):
    def __init__(self, kind: str):
        super().__init__(title={"whole_day": "Leave: whole day", "date_range": "Leave: date range",
                                "today_slots": "Leave: today's slots"}[kind])
        self.kind = kind
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300)
        self.frm = discord.ui.TextInput(label="From date YYYY-MM-DD" + ("" if kind == "date_range" else " (blank = today)"),
                                        required=kind == "date_range", max_length=10)
        self.to = discord.ui.TextInput(label="To date YYYY-MM-DD", required=True, max_length=10)
        self.add_item(self.reason)
        if kind != "today_slots":
            self.add_item(self.frm)
        if kind == "date_range":
            self.add_item(self.to)

    async def on_submit(self, i: discord.Interaction):
        await leave_apply.callback(i, app_commands.Choice(name=self.kind, value=self.kind), self.reason.value,
                                   self.frm.value.strip() or None if self.kind != "today_slots" else None,
                                   self.to.value.strip() or None if self.kind == "date_range" else None)


class PanelView(discord.ui.View):
    """Per-user (ephemeral) panel: moderators get row 1 only, admins get everything."""
    def __init__(self, admin: bool = False):
        super().__init__(timeout=840)
        if not admin:
            for child in list(self.children):
                if child.row != 0:
                    self.remove_item(child)

    # ---- row 0: everyone
    @discord.ui.button(label="My Slots", emoji="🕒", style=discord.ButtonStyle.primary, custom_id="att:myslots", row=0)
    async def b_myslots(self, i, b): await myslots.callback(i)

    @discord.ui.button(label="My Stats", emoji="📊", style=discord.ButtonStyle.primary, custom_id="att:mystats", row=0)
    async def b_mystats(self, i, b): await mystats.callback(i)

    @discord.ui.button(label="Apply Leave", emoji="🏖️", style=discord.ButtonStyle.success, custom_id="att:leave", row=0)
    async def b_leave(self, i, b):
        v = pick_view([discord.SelectOption(label="Whole day", value="whole_day"),
                       discord.SelectOption(label="Date range (2-3 days etc.)", value="date_range"),
                       discord.SelectOption(label="Specific slots today", value="today_slots")],
                      "What kind of leave?", lambda i2, val: i2.response.send_modal(LeaveModal(val)))
        await i.response.send_message("Choose leave type:", view=v, ephemeral=True)

    @discord.ui.button(label="My Leaves", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="att:myleaves", row=0)
    async def b_myleaves(self, i, b): await leave_list.callback(i)

    # ---- row 1: roster & slots (admin)
    @discord.ui.button(label="Add Mod", emoji="➕", style=discord.ButtonStyle.success, custom_id="att:addmod", row=1)
    async def b_addmod(self, i, b):
        if not await admin_only(i):
            return
        v = discord.ui.View(timeout=120)
        sel = discord.ui.UserSelect(placeholder="Pick moderator(s)", min_values=1, max_values=10)

        async def cb(i2: discord.Interaction):
            us = [u for u in sel.values if not u.bot]
            await q(sb.table("moderators").upsert(
                [{"guild_id": str(i2.guild_id), "user_id": str(u.id), "name": u.display_name} for u in us],
                on_conflict="guild_id,user_id"))
            await i2.response.edit_message(content="Added: " + ", ".join(u.display_name for u in us), view=None)
        sel.callback = cb
        v.add_item(sel)
        await i.response.send_message("Who should be added to the roster?", view=v, ephemeral=True)

    @discord.ui.button(label="Remove Mod", emoji="➖", style=discord.ButtonStyle.danger, custom_id="att:rmmod", row=1)
    async def b_rmmod(self, i, b):
        if not await admin_only(i):
            return
        opts = await roster_options(i.guild_id)
        if not opts:
            return await i.response.send_message("Roster is empty.", ephemeral=True)

        async def pick(i2, uid):
            await q(sb.table("moderators").delete().eq("guild_id", str(i2.guild_id)).eq("user_id", uid))
            await i2.response.edit_message(content=f"Removed <@{uid}> (their slots & history too).", view=None)
        await i.response.send_message("Remove which moderator?", view=pick_view(opts, "Choose moderator", pick),
                                      ephemeral=True)

    @discord.ui.button(label="Add Time Slot", emoji="🗓️", style=discord.ButtonStyle.success, custom_id="att:addslot", row=1)
    async def b_addslot(self, i, b):
        if not await admin_only(i):
            return
        opts = await roster_options(i.guild_id)
        if not opts:
            return await i.response.send_message("Add a moderator first.", ephemeral=True)

        async def pick(i2, uid):
            m = i2.guild.get_member(int(uid))
            if not m:
                return await i2.response.send_message("That moderator isn't in the server.", ephemeral=True)
            await i2.response.send_modal(SlotModal(m))
        await i.response.send_message("Add a slot for:", view=pick_view(opts, "Choose moderator", pick), ephemeral=True)

    @discord.ui.button(label="Remove Time Slot", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="att:rmslot", row=1)
    async def b_rmslot(self, i, b):
        if not await admin_only(i):
            return
        slots = await q(sb.table("slots").select("*, moderators(name)").eq("guild_id", str(i.guild_id)).order("id"))
        if not slots:
            return await i.response.send_message("No slots.", ephemeral=True)
        opts = [discord.SelectOption(label=f"#{s['id']} {s['moderators']['name']} · {slot_label(s)}"[:100],
                                     value=str(s["id"])) for s in slots[:25]]
        await i.response.send_message("Remove which slot?", ephemeral=True, view=pick_view(
            opts, "Choose slot", lambda i2, v: slot_remove.callback(i2, int(v))))

    @discord.ui.button(label="Roster", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="att:roster", row=1)
    async def b_roster(self, i, b):
        if await admin_only(i):
            await mod_list.callback(i)

    # ---- row 2: attendance (admin)
    @discord.ui.button(label="Mark", emoji="✍️", style=discord.ButtonStyle.primary, custom_id="att:mark", row=2)
    async def b_mark(self, i, b):
        if not await admin_only(i):
            return
        today = dt.datetime.now(ZoneInfo((await get_cfg(i.guild_id))["timezone"])).date()
        opts, by_id = await today_slot_options(i.guild_id, today)
        if not opts:
            return await i.response.send_message("No slots today.", ephemeral=True)

        async def pick(i2, sid):
            await i2.response.edit_message(content=f"Mark **{by_id[sid]['moderators']['name']}** · "
                                                   f"{slot_label(by_id[sid])} as:", view=StatusView(by_id[sid]))
        await i.response.send_message("Which slot?", ephemeral=True, view=pick_view(opts, "Choose today's slot", pick))

    @discord.ui.button(label="Today", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="att:today", row=2)
    async def b_today(self, i, b):
        if await admin_only(i):
            await att_today.callback(i)

    @discord.ui.button(label="Report", emoji="📈", style=discord.ButtonStyle.secondary, custom_id="att:report", row=2)
    async def b_report(self, i, b):
        if await admin_only(i):
            await att_report.callback(i)

    @discord.ui.button(label="Export CSV", emoji="📤", style=discord.ButtonStyle.secondary, custom_id="att:export", row=3)
    async def b_export(self, i, b):
        if await admin_only(i):
            await att_export.callback(i)

    @discord.ui.button(label="Settings", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="att:settings", row=3)
    async def b_settings(self, i, b):
        if not await admin_only(i):
            return
        c = await get_cfg(i.guild_id)
        await i.response.send_message(
            f"**Settings** — min **{c['min_minutes']}** min · {c['timezone']} · mode **{c['mode']}** · "
            f"reminder **{c['reminder_minutes']}** min\nApprovers: {fmt_approvers(c.get('leave_approvers'))}",
            view=SettingsView(), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---- row 3: leave inbox (admin)
    @discord.ui.button(label="Pending Leaves", emoji="📥", style=discord.ButtonStyle.secondary, custom_id="att:pending", row=0)
    async def b_pending(self, i, b):
        admin = i.user.guild_permissions.manage_guild
        req = (sb.table("leaves").select("*, moderators(name)").eq("guild_id", str(i.guild_id))
               .eq("status", "pending"))
        if not admin:
            req = req.eq("user_id", str(i.user.id))
        rows = await q(req.order("id"))
        txt = "\n".join(f"`#{r['id']}` **{r['moderators']['name']}** — {r['start_date']}"
                        f"{'' if r['start_date'] == r['end_date'] else ' → ' + r['end_date']}"
                        f"{' (slots)' if r['slot_ids'] else ''} — {r['reason'] or ''}" for r in rows)
        note = "\n_Approve/Reject using the buttons on each request in the leave channel._" if rows and admin else ""
        await i.response.send_message((txt or "No pending leave requests. 🎉") + note, ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="att:refresh", row=3)
    async def b_refresh(self, i, b): await i.response.edit_message(embed=await panel_embed(i.guild))


class LauncherView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Panel", emoji="🛡️", style=discord.ButtonStyle.primary, custom_id="att:open")
    async def open_panel(self, i, b):
        await i.response.send_message(embed=await panel_embed(i.guild), ephemeral=True,
                                      view=PanelView(i.user.guild_permissions.manage_guild))


@tree.command(name="panel", description="Open your Attendis panel")
@app_commands.describe(public="Post a public 'Open Panel' button for everyone (admins only)")
@app_commands.guild_only()
async def panel(i: discord.Interaction, public: bool = False):
    if public:
        if not i.user.guild_permissions.manage_guild:
            return await i.response.send_message("Only admins can post the public panel.", ephemeral=True)
        return await i.response.send_message(embed=await panel_embed(i.guild), view=LauncherView())
    await i.response.send_message(embed=await panel_embed(i.guild), ephemeral=True,
                                  view=PanelView(i.user.guild_permissions.manage_guild))


@bot.event
async def on_message(m: discord.Message):
    if TRIGGER and m.guild and not m.author.bot and m.content.strip().lower() == TRIGGER:
        await m.channel.send(embed=await panel_embed(m.guild), view=LauncherView())


for g in (mod_g, slot_g, att_g, cfg_g, leave_g):
    tree.add_command(g)


@bot.event
async def on_ready():
    global _keepalive_started
    if not _keepalive_started:
        _keepalive_started = True
        await start_keepalive()
    bot.add_dynamic_items(LeaveBtn)
    bot.add_view(LauncherView())
    await tree.sync()
    if not tracker.is_running():
        tracker.start()
    if not cleanup.is_running():
        cleanup.start()
    print(f"Logged in as {bot.user}")


bot.run(os.environ["DISCORD_TOKEN"])
